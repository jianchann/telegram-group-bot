"""Phase 2 lifecycle through real storage and scripted provider/Telegram boundaries."""

import json
import os
from collections.abc import Callable
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select

from app.ai.tools.memory import MemoryToolExecutor
from app.db.models import AIRun, Memory, Message, UpdateProcessing
from app.db.session import Database
from app.repositories.bot import BotRepository
from app.schemas.ai import GeminiTurn, ToolCall
from app.services.ai_orchestrator import AIOrchestrator
from app.services.context_service import ContextService
from app.services.memory_service import MemoryService
from app.services.telegram_service import TelegramService

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]
CallBuilder = Callable[[dict[str, Any]], list[ToolCall]]


class ScriptedGateway:
    def __init__(self):
        self.steps = []
        self.prompts = []
        self.tool_results = []
        self.call_count = 0

    async def turn(self, **kwargs):
        self.call_count += 1
        prompt = json.loads(kwargs["prompt"])
        self.prompts.append(prompt)
        if kwargs.get("results"):
            self.tool_results.extend(kwargs["results"])
        assert self.steps, "Unplanned provider call"
        step = self.steps.pop(0)
        if callable(step):
            return GeminiTurn(calls=step(prompt), input_tokens=10, output_tokens=5)
        return GeminiTurn(text=step, input_tokens=10, output_tokens=5)


class Pipeline:
    def __init__(self, settings, database):
        self.database = database
        self.repository = BotRepository(database)
        self.memories = MemoryService(database)
        self.gateway = ScriptedGateway()
        self.next_message_id = 0
        self.next_update_id = 0
        self.sent = []
        self.service = TelegramService(
            settings.model_copy(
                update={
                    "ai_user_requests_per_minute": 100,
                    "ai_chat_requests_per_minute": 100,
                }
            ),
            self.repository,
            ContextService(self.repository, 30, 24000, self.memories),
            AIOrchestrator(
                self.gateway,
                "Application memory rules",
                lambda context: MemoryToolExecutor(context, self.memories),
            ),
            self,
            42,
            "groupbot",
            self.memories,
        )

    async def send_message(self, chat_id, text, reply_to_message_id=None, parse_mode="HTML"):
        self.next_message_id += 1
        payload = {
            "message_id": self.next_message_id,
            "date": 1700000000,
            "chat": {"id": chat_id, "type": "supergroup", "title": "Group"},
            "from": {"id": 42, "first_name": "Bot", "is_bot": True},
            "text": text,
        }
        self.sent.append(payload)
        return payload

    async def incoming(self, text, invoke=False, builder=None):
        self.next_update_id += 1
        self.next_message_id += 1
        update = {
            "update_id": self.next_update_id,
            "message": {
                "message_id": self.next_message_id,
                "date": 1700000000,
                "chat": {"id": -1001, "type": "supergroup", "title": "Group"},
                "from": {"id": 99, "first_name": "Jian", "is_bot": False},
                "text": f"/ask {text}" if invoke else text,
                "entities": [{"type": "bot_command", "offset": 0, "length": 4}] if invoke else [],
            },
        }
        if invoke:
            self.gateway.steps = (
                [builder, "Request completed."] if builder else ["Here is the group's context."]
            )
        await self.service.process(update)
        assert not self.gateway.steps
        return update

    async def active_memories(self):
        async with self.database.sessions() as session:
            return list(
                (await session.execute(select(Memory).where(Memory.status == "active"))).scalars()
            )


def upgrade_all(connection):
    directory = Path(__file__).parents[1] / "alembic/versions"
    for path in sorted(directory.glob("[0-9]*.py")):
        spec = spec_from_file_location(path.stem, path)
        migration = module_from_spec(spec)
        spec.loader.exec_module(migration)
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()


@pytest_asyncio.fixture
async def pipeline(settings):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for the real PostgreSQL memory pipeline")
    database = Database(url)
    schema = "memory_pipeline_" + uuid4().hex
    async with database.engine.begin() as connection:
        await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    database.engine.update_execution_options(schema_translate_map={None: schema})
    try:
        async with database.engine.begin() as connection:
            await connection.run_sync(upgrade_all)
        yield Pipeline(settings, database)
    finally:
        async with database.engine.begin() as connection:
            await connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        await database.close()


def save_fact(content, quote, key="flight-time", explicit=False):
    def build(prompt):
        return [
            ToolCall(
                "save_memory",
                {
                    "scope": "group",
                    "content": content,
                    "category": "preference",
                    "normalized_key": key,
                    "source_quote": quote,
                    "source_message_id": prompt["current_source_message_id"],
                    "durability": "explicit_request" if explicit else "preference",
                },
                "save-1",
            )
        ]

    return build


async def test_memory_lifecycle_through_context_tools_and_telegram(pipeline):
    passive = await pipeline.incoming("We always prefer morning flights.")
    assert pipeline.gateway.call_count == 0
    assert pipeline.sent == []
    assert await pipeline.active_memories() == []

    await pipeline.incoming(
        "Remember we prefer morning flights",
        True,
        save_fact("We prefer morning flights", "we prefer morning flights", explicit=True),
    )
    original = (await pipeline.active_memories())[0]
    assert pipeline.gateway.tool_results[-1].result["action"] == "saved"
    assert "We prefer morning flights" in pipeline.sent[-1]["text"]

    await pipeline.incoming(
        "What do you remember about our flights?",
        True,
        lambda _: [ToolCall("search_memories", {"query": "morning flights"}, "search-1")],
    )
    assert pipeline.gateway.prompts[-2]["memories"][0]["id"] == str(original.id)
    assert pipeline.gateway.tool_results[-1].result["memories"][0]["id"] == str(original.id)

    await pipeline.incoming(
        "Correct our morning flights preference to evening flights",
        True,
        lambda _: [
            ToolCall(
                "update_memory",
                {
                    "memory_id": str(original.id),
                    "content": "We prefer evening flights",
                    "source_quote": "evening flights",
                },
                "update-1",
            )
        ],
    )
    updated = (await pipeline.active_memories())[0]
    assert updated.id == original.id
    assert updated.content == "We prefer evening flights"
    assert pipeline.gateway.tool_results[-1].result["action"] == "updated"

    await pipeline.incoming(
        "Forget our evening flights preference",
        True,
        lambda _: [ToolCall("delete_memory", {"memory_id": str(original.id)}, "delete-1")],
    )
    assert await pipeline.active_memories() == []
    assert pipeline.gateway.tool_results[-1].result["action"] == "deleted"

    await pipeline.incoming(
        "We always prefer evening flights",
        True,
        save_fact("We prefer evening flights", "We always prefer evening flights"),
    )
    assert pipeline.gateway.tool_results[-1].result["action"] == "suppressed"
    assert await pipeline.active_memories() == []

    await pipeline.incoming(
        "Remember we prefer evening flights",
        True,
        save_fact("We prefer evening flights", "we prefer evening flights", explicit=True),
    )
    resaved = (await pipeline.active_memories())[0]
    assert resaved.id != original.id
    assert pipeline.gateway.tool_results[-1].result["action"] == "saved"
    assert pipeline.gateway.tool_results[-1].call_id == "save-1"

    # Replaying a saved passive update cannot call the provider or recreate a memory.
    calls, replies = pipeline.gateway.call_count, len(pipeline.sent)
    await pipeline.service.process(passive)
    assert (pipeline.gateway.call_count, len(pipeline.sent)) == (calls, replies)
    async with pipeline.database.sessions() as session:
        assert (await session.execute(select(func.count()).select_from(AIRun))).scalar_one() == 6
        assert (
            await session.execute(select(func.count()).select_from(UpdateProcessing))
        ).scalar_one() == 7
        assert (await session.execute(select(func.count()).select_from(Message))).scalar_one() == 13
        deleted = await session.get(Memory, original.id)
        assert deleted.status == "deleted"
        assert resaved.source_message_id is not None


async def test_successful_memory_invocation_is_idempotent_and_commands_are_provider_free(pipeline):
    update = await pipeline.incoming(
        "Remember we prefer morning flights",
        True,
        save_fact("We prefer morning flights", "we prefer morning flights", explicit=True),
    )
    count, replies = pipeline.gateway.call_count, len(pipeline.sent)
    await pipeline.service.process(update)
    assert pipeline.gateway.call_count == count
    assert len(pipeline.sent) == replies
    assert len(await pipeline.active_memories()) == 1
    await pipeline.incoming("/memory")
    assert pipeline.gateway.call_count == count
    assert "We prefer morning flights" in pipeline.sent[-1]["text"]
    async with pipeline.database.sessions() as session:
        assert (await session.execute(select(func.count()).select_from(AIRun))).scalar_one() == 1


async def test_automatic_memory_uses_real_passive_evidence_and_supersedes_conflicts(pipeline):
    passive = await pipeline.incoming("We always prefer morning flights")
    assert await pipeline.active_memories() == []

    def remember_prior_source(prompt):
        source = next(
            message
            for message in prompt["recent_group_context"]
            if message["message_id"] == passive["message"]["message_id"]
        )
        return [
            ToolCall(
                "save_memory",
                {
                    "scope": "group",
                    "content": "We prefer morning flights",
                    "category": "preference",
                    "normalized_key": "flight-time",
                    "source_quote": "We always prefer morning flights",
                    "source_message_id": source["source_message_id"],
                    "durability": "preference",
                },
                "prior-source",
            )
        ]

    await pipeline.incoming("What flights should we choose?", True, remember_prior_source)
    original = (await pipeline.active_memories())[0]
    async with pipeline.database.sessions() as session:
        source = await session.get(Message, original.source_message_id)
        assert source.telegram_message_id == passive["message"]["message_id"]
        assert original.created_by_model
    await pipeline.incoming(
        "We always prefer evening flights",
        True,
        save_fact("We prefer evening flights", "We always prefer evening flights"),
    )
    replacement = (await pipeline.active_memories())[0]
    assert replacement.id != original.id
    assert replacement.content == "We prefer evening flights"
    async with pipeline.database.sessions() as session:
        old = await session.get(Memory, original.id)
        assert old.status == "superseded"
        assert old.latest_source_message_id == replacement.source_message_id
