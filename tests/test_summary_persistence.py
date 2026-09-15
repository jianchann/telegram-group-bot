"""Real PostgreSQL rolling-summary selection and concurrency checks."""

import asyncio
import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.db.session import Database
from app.repositories.bot import BotRepository
from app.repositories.summaries import SummaryRepository
from app.schemas.summary import SummaryConflict
from app.services.summary_service import SummaryService

pytestmark = pytest.mark.asyncio


def apply_migrations(connection):
    directory = Path(__file__).resolve().parents[1] / "alembic/versions"
    for path in sorted(directory.glob("[0-9]*.py")):
        spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()


def message(message_id, chat_id=-1001, text=None):
    return {
        "message_id": message_id,
        "date": 1_700_000_000 + message_id,
        "chat": {"id": chat_id, "type": "supergroup", "title": "Test"},
        "from": {"id": 100 + message_id % 2, "first_name": f"Member {message_id % 2}"},
        "text": text or f"Message {message_id}",
    }


@pytest_asyncio.fixture
async def summary_environment():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for real PostgreSQL summary tests")
    database = Database(url)
    schema = "test_summary_" + uuid4().hex
    async with database.engine.begin() as connection:
        await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    database.engine.update_execution_options(schema_translate_map={None: schema})
    async with database.engine.begin() as connection:
        await connection.run_sync(apply_migrations)
    try:
        yield database, BotRepository(database), SummaryRepository(database)
    finally:
        async with database.engine.begin() as connection:
            await connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        await database.close()


async def test_selection_preserves_newest_messages_and_advances_checkpoint(summary_environment):
    _, bot, repository = summary_environment
    accepted = None
    for message_id in range(1, 36):
        accepted = await bot.accept_message(message_id, message(message_id), "Asia/Manila")
    service = SummaryService(repository, message_threshold=1, preserve_recent=30)
    batch = await service.prepare(accepted.chat_id, 36)
    assert batch.unsummarized_count == 35
    assert [entry.telegram_message_id for entry in batch.messages] == [1, 2, 3, 4, 5]
    saved = await service.save(
        accepted.chat_id, None, "Ongoing topics\nPlanning", batch.messages[-1].id
    )
    assert saved.version == 1
    next_batch = await service.prepare(accepted.chat_id, 36)
    assert next_batch.unsummarized_count == 30
    assert not next_batch.should_summarize


async def test_compare_and_swap_allows_only_one_concurrent_writer(summary_environment):
    _, bot, repository = summary_environment
    accepted = await bot.accept_message(100, message(1), "Asia/Manila")
    service = SummaryService(repository, preserve_recent=0)
    initial = await service.save(accepted.chat_id, None, "Initial", accepted.message_id)
    results = await asyncio.gather(
        service.save(accepted.chat_id, initial.version, "First", accepted.message_id),
        service.save(accepted.chat_id, initial.version, "Second", accepted.message_id),
        return_exceptions=True,
    )
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, SummaryConflict) for result in results) == 1


async def test_summary_state_and_messages_are_chat_scoped(summary_environment):
    _, bot, repository = summary_environment
    first = await bot.accept_message(200, message(1), "Asia/Manila")
    foreign = await bot.accept_message(201, message(1, -1002), "Asia/Manila")
    service = SummaryService(repository, message_threshold=0, preserve_recent=0)
    first_batch = await service.prepare(first.chat_id, 2)
    await service.save(first.chat_id, None, "First chat", first_batch.messages[-1].id)
    assert (await repository.get(first.chat_id)).summary == "First chat"
    assert await repository.get(foreign.chat_id) is None
    foreign_batch = await service.prepare(foreign.chat_id, 2)
    assert [entry.text for entry in foreign_batch.messages] == ["Message 1"]
