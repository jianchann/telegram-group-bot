"""Real PostgreSQL checks; never silently substitute SQLite."""

import asyncio
import importlib.util
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import MetaData, func, select, update

from app.db.models import Base, Chat, Message
from app.db.session import Database
from app.repositories.bot import BotRepository

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def repository():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for real PostgreSQL integration tests")
    database = Database(url)
    # Isolated schema prevents dropping or changing any pre-existing tables.
    schema = "test_" + uuid4().hex
    async with database.engine.begin() as connection:
        await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    database.engine.update_execution_options(schema_translate_map={None: schema})
    async with database.engine.begin() as connection:
        await connection.run_sync(_apply_initial_migration)
    try:
        yield BotRepository(database)
    finally:
        async with database.engine.begin() as connection:
            await connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        await database.close()


def _apply_initial_migration(connection):
    directory = Path(__file__).resolve().parents[1] / "alembic/versions"
    for path in sorted(directory.glob("[0-9]*.py")):
        spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()


def message(message_id=1, chat_id=-1001, sender_id=99):
    return {
        "message_id": message_id,
        "date": int(datetime.now(UTC).timestamp()),
        "chat": {"id": chat_id, "type": "supergroup", "title": "Test"},
        "from": {"id": sender_id, "first_name": "Ada"},
        "text": f"Message {message_id}",
    }


async def test_concurrent_duplicates_are_claimed_once(repository):
    results = await asyncio.gather(
        *(repository.accept_message(10, message(), "Asia/Manila") for _ in range(8))
    )
    assert sum(result is not None for result in results) == 1
    async with repository.database.sessions() as session:
        assert (await session.execute(select(func.count()).select_from(Message))).scalar_one() == 1
    assert await repository.accept_message(11, message(), "Asia/Manila") is None


async def test_history_is_chronological_and_chat_scoped(repository):
    accepted = await repository.accept_message(20, message(1), "Asia/Manila")
    await repository.accept_message(21, message(2, -1002), "Asia/Manila")
    await repository.accept_message(22, message(3), "Asia/Manila")
    await repository.save_outgoing(accepted.chat_id, message(4, sender_id=100))
    rows = await repository.recent_messages(accepted.chat_id, 5, 2)
    assert [row.telegram_message_id for row in rows] == [3, 4]
    assert rows[-1].is_bot_message
    assert rows[0].created_at.tzinfo is not None


async def test_telegram_action_target_requires_current_member_and_source(repository):
    accepted = await repository.accept_message(23, message(1), "Asia/Manila")
    foreign = await repository.accept_message(24, message(1, -1002, 101), "Asia/Manila")
    assert (
        await repository.telegram_chat_id(accepted.chat_id, accepted.user_id, accepted.message_id)
        == -1001
    )
    assert (
        await repository.telegram_chat_id(accepted.chat_id, foreign.user_id, foreign.message_id)
        is None
    )


async def test_concurrent_rate_limit_reservations(repository):
    accepted = await repository.accept_message(30, message(), "Asia/Manila")
    results = await asyncio.gather(
        *(
            repository.reserve_ai_request(accepted.chat_id, accepted.user_id, 3, 10)
            for _ in range(8)
        )
    )
    assert sum(results) == 3
    other = await repository.accept_message(31, message(2, sender_id=101), "Asia/Manila")
    assert not await repository.reserve_ai_request(other.chat_id, other.user_id, 10, 3)


async def test_anonymous_sender_metadata_is_preserved(repository):
    payload = message()
    payload["sender_chat"] = {"id": -1001, "title": "Anonymous admin"}
    accepted = await repository.accept_message(40, payload, "Asia/Manila")
    assert accepted.user_id is None
    rows = await repository.recent_messages(accepted.chat_id, 2, 30)
    assert rows[0].display_name == "Anonymous admin"


async def test_failed_persistence_rolls_back_processing_claim(repository):
    invalid = message()
    invalid["date"] = "invalid timestamp"
    with pytest.raises(TypeError):
        await repository.accept_message(50, invalid, "Asia/Manila")
    accepted = await repository.accept_message(50, message(), "Asia/Manila")
    assert accepted is not None


async def test_disabled_chat_still_persists_messages(repository):
    accepted = await repository.accept_message(60, message(), "Asia/Manila")
    assert accepted.bot_enabled
    async with repository.database.sessions.begin() as session:
        await session.execute(
            update(Chat).where(Chat.id == accepted.chat_id).values(bot_enabled=False)
        )
    disabled = await repository.accept_message(61, message(2), "Asia/Manila")
    assert disabled is not None
    assert not disabled.bot_enabled
    assert len(await repository.recent_messages(disabled.chat_id, 3, 30)) == 2


async def test_migration_matches_models(repository):
    async with repository.database.engine.connect() as connection:
        schema = connection.sync_connection.get_execution_options()["schema_translate_map"][None]
        expected = MetaData()
        for table in Base.metadata.sorted_tables:
            copied = table.to_metadata(expected, schema=schema)
            # Moving metadata to a test schema must preserve production index names.
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

        differences = await connection.run_sync(compare)
        assert differences == [], repr(differences)
