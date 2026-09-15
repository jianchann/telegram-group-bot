"""Real PostgreSQL memory isolation, concurrency, lifecycle, and migration checks."""

import asyncio
import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import MetaData, func, select
from sqlalchemy.exc import SQLAlchemyError

from app.db.models import Base, Memory, Message
from app.db.session import Database
from app.repositories.bot import BotRepository
from app.schemas.memory import MemoryDomainError, MemoryInfrastructureError
from app.services.memory_service import MemoryService

pytestmark = pytest.mark.asyncio


def apply_migrations(connection):
    directory = Path(__file__).resolve().parents[1] / "alembic/versions"
    for path in sorted(directory.glob("[0-9]*.py")):
        spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()


@pytest_asyncio.fixture
async def memory_environment():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for real PostgreSQL memory tests")
    database = Database(url)
    schema = "test_memory_" + uuid4().hex
    async with database.engine.begin() as connection:
        await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    database.engine.update_execution_options(schema_translate_map={None: schema})
    async with database.engine.begin() as connection:
        await connection.run_sync(apply_migrations)
    bot = BotRepository(database)
    first = await bot.accept_message(1, message(1, -1001, 101), "Asia/Manila")
    second = await bot.accept_message(2, message(2, -1001, 102), "Asia/Manila")
    foreign = await bot.accept_message(3, message(1, -1002, 103), "Asia/Manila")
    try:
        yield MemoryService(database), database, first, second, foreign
    finally:
        async with database.engine.begin() as connection:
            await connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        await database.close()


def message(message_id, chat_id, user_id):
    return {
        "message_id": message_id,
        "date": 1700000000,
        "chat": {"id": chat_id, "type": "supergroup", "title": "Test group"},
        "from": {"id": user_id, "first_name": f"Member {user_id}", "is_bot": False},
        "text": "Remember we prefer morning flights",
    }


async def test_concurrent_saves_deduplicate_and_scope_isolated(memory_environment):
    service, database, first, second, _ = memory_environment
    results = await asyncio.gather(
        *(
            service.save(
                first.chat_id,
                first.user_id,
                first.message_id,
                "group",
                "Morning flights",
                normalized_key="flight-time",
            )
            for _ in range(8)
        )
    )
    assert sum(result.action == "saved" for result in results) == 1
    duplicate = await service.save(
        first.chat_id, second.user_id, second.message_id, "group", "  MORNING   flights  "
    )
    assert duplicate.action == "unchanged"
    personal = await service.save(
        first.chat_id,
        first.user_id,
        first.message_id,
        "user",
        "Morning flights",
        user_id=second.user_id,
    )
    assert personal.action == "saved"
    async with database.sessions() as session:
        assert (await session.execute(select(func.count()).select_from(Memory))).scalar_one() == 2


async def test_key_conflicts_supersede_and_deletions_suppress_autosave(memory_environment):
    service, database, first, second, _ = memory_environment
    original = await service.save(
        first.chat_id,
        first.user_id,
        first.message_id,
        "group",
        "Morning flights",
        normalized_key="flight-time",
    )
    latest = await service.save(
        first.chat_id,
        second.user_id,
        second.message_id,
        "group",
        "Evening flights",
        normalized_key="FLIGHT-TIME",
    )
    async with database.sessions() as session:
        old = await session.get(Memory, original.memory.id)
        assert old.status == "superseded"
        assert old.latest_actor_user_id == second.user_id
    assert len(await service.search(first.chat_id)) == 1
    deleted = await service.delete(first.chat_id, first.user_id, first.message_id, latest.memory.id)
    assert deleted.action == "deleted"
    assert await service.search(first.chat_id) == []
    suppressed = await service.save(
        first.chat_id,
        first.user_id,
        first.message_id,
        "group",
        "Afternoon flights",
        normalized_key="flight-time",
    )
    assert suppressed.action == "suppressed"
    suppressed_content = await service.save(
        first.chat_id, first.user_id, first.message_id, "group", "Evening flights"
    )
    assert suppressed_content.action == "suppressed"
    resaved = await service.save(
        first.chat_id,
        first.user_id,
        first.message_id,
        "group",
        "Evening flights",
        normalized_key="flight-time",
        explicit=True,
    )
    assert resaved.action == "saved"
    assert resaved.memory.id != latest.memory.id


async def test_cross_chat_actor_source_target_and_member_are_rejected(memory_environment):
    service, _, first, second, foreign = memory_environment
    saved = await service.save(
        first.chat_id, first.user_id, first.message_id, "group", "Morning flights"
    )
    invalid_operations = [
        service.save(first.chat_id, foreign.user_id, first.message_id, "group", "Bad actor"),
        service.save(first.chat_id, first.user_id, foreign.message_id, "group", "Bad source"),
        service.save(
            first.chat_id,
            first.user_id,
            first.message_id,
            "user",
            "Bad member",
            user_id=foreign.user_id,
        ),
        service.search(first.chat_id, user_id=foreign.user_id),
        service.update(
            foreign.chat_id,
            foreign.user_id,
            foreign.message_id,
            saved.memory.id,
            content="Bad target",
        ),
        service.delete(foreign.chat_id, foreign.user_id, foreign.message_id, saved.memory.id),
        service.sources(first.chat_id, [9999]),
    ]
    results = await asyncio.gather(*invalid_operations, return_exceptions=True)
    assert all(isinstance(result, MemoryDomainError) for result in results)
    assert (await service.search(first.chat_id))[0].content == "Morning flights"
    # Any observed member may edit another member's current-chat personal memory.
    personal = await service.save(
        first.chat_id,
        first.user_id,
        first.message_id,
        "user",
        "Aisle seats",
        user_id=second.user_id,
    )
    edited = await service.update(
        first.chat_id,
        first.user_id,
        first.message_id,
        personal.memory.id,
        content="Window seats",
        importance=8,
    )
    assert edited.action == "updated"
    assert edited.memory.user_id == second.user_id


async def test_listing_sources_settings_and_json_contracts(memory_environment):
    service, database, first, second, _ = memory_environment
    for index in range(3):
        await service.save(
            first.chat_id, first.user_id, first.message_id, "group", f"Group memory {index}"
        )
    await service.save(
        first.chat_id,
        first.user_id,
        first.message_id,
        "user",
        "Personal preference",
        user_id=second.user_id,
    )
    entries, total = await service.list_page(first.chat_id, page=2, page_size=2)
    assert total == 4
    assert len(entries) == 2
    personal, total = await service.list_page(first.chat_id, user_id=second.user_id)
    assert total == 1
    assert personal[0].member_name == "Member 102"
    assert isinstance(personal[0].as_dict()["id"], str)
    assert len(await service.members(first.chat_id)) == 2
    assert len(await service.sources(first.chat_id, [1, 2])) == 2
    assert (await service.settings(first.chat_id))["auto_memory_enabled"] is True
    # Escaped SQL LIKE metacharacters must be treated as literal query text.
    assert await service.search(first.chat_id, query="%") == []
    async with database.sessions() as session:
        memory = await session.get(Memory, personal[0].id)
        assert memory.created_at.tzinfo is not None


async def test_bot_sources_and_bot_members_are_rejected(memory_environment):
    service, database, first, _, _ = memory_environment
    bot = BotRepository(database)
    payload = message(4, -1001, 999)
    payload["from"]["is_bot"] = True
    outgoing = await bot.accept_message(4, payload, "Asia/Manila")
    with pytest.raises(MemoryDomainError, match="source_not_found"):
        await service.save(first.chat_id, first.user_id, outgoing.message_id, "group", "Bot claim")
    with pytest.raises(MemoryDomainError, match="member_not_found"):
        await service.save(first.chat_id, outgoing.user_id, first.message_id, "group", "Bot actor")
    assert len(await service.members(first.chat_id)) == 2


async def test_validation_and_infrastructure_error_contract(memory_environment, monkeypatch):
    service, _, first, _, _ = memory_environment
    with pytest.raises(MemoryDomainError, match="invalid_scope"):
        await service.save(first.chat_id, first.user_id, first.message_id, "user", "Missing target")
    with pytest.raises(MemoryDomainError, match="invalid_importance"):
        await service.save(
            first.chat_id, first.user_id, first.message_id, "group", "Fact", importance=11
        )

    async def broken_search(*args, **kwargs):
        raise SQLAlchemyError("not exposed to user")

    monkeypatch.setattr(service.repository, "search", broken_search)
    with pytest.raises(MemoryInfrastructureError, match="database_error"):
        await service.search(first.chat_id)


async def test_memory_migration_matches_models(memory_environment):
    _, database, _, _, _ = memory_environment
    async with database.engine.connect() as connection:
        schema = connection.sync_connection.get_execution_options()["schema_translate_map"][None]
        expected = MetaData()
        for table in Base.metadata.sorted_tables:
            copied = table.to_metadata(expected, schema=schema)
            for index in copied.indexes:
                columns = tuple(column.name for column in index.columns)
                original = next(
                    candidate
                    for candidate in table.indexes
                    if tuple(column.name for column in candidate.columns) == columns
                )
                index.name = original.name

        def compare(sync_connection):
            context = MigrationContext.configure(
                sync_connection,
                opts={
                    "include_schemas": True,
                    "include_name": lambda name, kind, parents: kind != "schema" or name == schema,
                },
            )
            return compare_metadata(context, expected)

        assert await connection.run_sync(compare) == []


async def test_search_matches_keywords_before_importance_and_handles_wildcards(memory_environment):
    service, _, first, second, _ = memory_environment
    for index in range(25):
        await service.save(
            first.chat_id,
            first.user_id,
            first.message_id,
            "group",
            f"Flights logistics {index}",
            importance=10,
        )
    await service.save(
        first.chat_id,
        first.user_id,
        first.message_id,
        "user",
        "Anna is vegetarian",
        category="food",
        user_id=second.user_id,
        normalized_key="anna.diet",
        importance=1,
    )
    rows = await service.search(
        first.chat_id, query="what do you remember about Anna vegetarian flights", limit=20
    )
    assert len(rows) == 20
    assert rows[0].content == "Anna is vegetarian"
    assert await service.search(first.chat_id, query="%") == []
    assert await service.search(first.chat_id, query="_") == []
    literal = await service.save(
        first.chat_id, first.user_id, first.message_id, "group", "Budget includes 10% allowance"
    )
    assert [entry.id for entry in await service.search(first.chat_id, query="%")] == [
        literal.memory.id
    ]
    assert len(await service.search(first.chat_id, query="what do you remember about us")) == 20


async def test_sources_include_real_reply_links_and_none_sender_is_safe(memory_environment):
    service, database, first, _, _ = memory_environment
    bot = BotRepository(database)
    payload = message(5, -1001, 101)
    payload["reply_to_message"] = {"message_id": 1}
    await bot.accept_message(5, payload, "Asia/Manila")
    source = (await service.sources(first.chat_id, [5]))[0]
    assert source.reply_to_telegram_message_id == 1
    async with database.sessions.begin() as session:
        stored = await session.get(Message, first.message_id)
        stored.raw_payload = {**stored.raw_payload, "from": None}
    assert not (await service.sources(first.chat_id, [1]))[0].is_bot_message
