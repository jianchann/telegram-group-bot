"""HTTP contract against migrated PostgreSQL, with all paid/external calls fake."""

import os
from datetime import UTC, datetime
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select

from app.db.models import AIRun, Message, UpdateProcessing
from app.db.session import Database
from app.main import create_app
from app.repositories.bot import BotRepository
from app.schemas.ai import GeminiResult
from app.services.ai_orchestrator import AIOrchestrator
from app.services.context_service import ContextService
from app.services.telegram_service import TelegramService

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_full_webhook_pipeline_against_migrated_postgres(settings):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for real PostgreSQL integration tests")
    database = Database(url)
    schema = "webhook_test_" + uuid4().hex
    async with database.engine.begin() as connection:
        await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    database.engine.update_execution_options(schema_translate_map={None: schema})
    migration_path = Path(__file__).parents[1] / "alembic" / "versions" / "0001_phase1.py"
    spec = spec_from_file_location("phase1_migration", migration_path)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)

    def upgrade(connection):
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()

    try:
        async with database.engine.begin() as connection:
            await connection.run_sync(upgrade)
        repository = BotRepository(database)
        gateway = SimpleNamespace(
            run=AsyncMock(return_value=GeminiResult("Hongdae was proposed.", 10, 4))
        )
        telegram = SimpleNamespace(
            send_message=AsyncMock(
                return_value={
                    "message_id": 3,
                    "date": int(datetime.now(UTC).timestamp()),
                    "chat": {"id": -1001, "type": "supergroup"},
                    "from": {"id": 42, "first_name": "Bot", "is_bot": True},
                    "text": "Hongdae was proposed.",
                }
            )
        )
        service = TelegramService(
            settings,
            repository,
            ContextService(repository, 30, 24000),
            AIOrchestrator(gateway, "Application rules"),
            telegram,
            42,
            "groupbot",
        )
        app = create_app(settings, service)
        headers = {
            "X-Telegram-Bot-Api-Secret-Token": settings.telegram_webhook_secret.get_secret_value()
        }

        def incoming(update_id, message_id, text, entities=None):
            return {
                "update_id": update_id,
                "message": {
                    "message_id": message_id,
                    "date": int(datetime.now(UTC).timestamp()),
                    "chat": {"id": -1001, "type": "supergroup"},
                    "from": {"id": 99, "first_name": "Jian"},
                    "text": text,
                    "entities": entities or [],
                },
            }

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            passive = incoming(1, 1, "Maybe Hongdae for our trip?")
            assert (
                await client.post("/telegram/webhook", json=passive, headers=headers)
            ).status_code == 200
            gateway.run.assert_not_awaited()
            telegram.send_message.assert_not_awaited()
            invocation = incoming(
                2,
                2,
                "@groupbot what did we propose?",
                [{"type": "mention", "offset": 0, "length": 9}],
            )
            assert (
                await client.post("/telegram/webhook", json=invocation, headers=headers)
            ).status_code == 200
            assert (
                await client.post("/telegram/webhook", json=invocation, headers=headers)
            ).status_code == 200
        gateway.run.assert_awaited_once()
        assert "Maybe Hongdae for our trip?" in gateway.run.call_args.kwargs["prompt"]
        telegram.send_message.assert_awaited_once()
        async with database.sessions() as session:
            assert (
                await session.execute(select(func.count()).select_from(Message))
            ).scalar_one() == 3
            assert (
                await session.execute(select(func.count()).select_from(AIRun))
            ).scalar_one() == 1
            assert (
                await session.execute(select(func.count()).select_from(UpdateProcessing))
            ).scalar_one() == 2
    finally:
        async with database.engine.begin() as connection:
            await connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        await database.close()
