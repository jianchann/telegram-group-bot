"""Real-Postgres Telegram-to-plan lifecycle tests with scripted external boundaries."""

import json
import os
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select

from app.ai.tools.composite import CompositeToolExecutor
from app.ai.tools.memory import MemoryToolExecutor
from app.ai.tools.plan import PlanToolExecutor
from app.db.models import AIRun, Plan, PlanItem, UpdateProcessing
from app.db.session import Database
from app.repositories.bot import BotRepository
from app.schemas.ai import GeminiTurn, ToolCall
from app.services.ai_orchestrator import AIOrchestrator
from app.services.context_service import ContextService
from app.services.memory_service import MemoryService
from app.services.plan_service import PlanService
from app.services.telegram_service import TelegramService

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


class Gateway:
    def __init__(self):
        self.steps, self.results, self.prompts = [], [], []
        self.calls = 0

    async def turn(self, **kwargs):
        self.calls += 1
        prompt = json.loads(kwargs["prompt"])
        self.prompts.append(prompt)
        self.results.extend(kwargs.get("results") or [])
        step = self.steps.pop(0)
        if callable(step):
            return GeminiTurn(calls=step(prompt), input_tokens=3, output_tokens=2)
        return GeminiTurn(text=step, input_tokens=3, output_tokens=2)


class Pipeline:
    def __init__(self, settings, database):
        self.database = database
        self.repo = BotRepository(database)
        self.memory, self.plans = MemoryService(database), PlanService(database)
        self.gateway, self.sent = Gateway(), []
        self.message_id = self.update_id = 0
        context = ContextService(self.repo, 30, 48000, self.memory, plan_service=self.plans)

        def tools(ctx):
            return CompositeToolExecutor(
                MemoryToolExecutor(ctx, self.memory), PlanToolExecutor(ctx, self.plans)
            )

        configured = settings.model_copy(
            update={"ai_user_requests_per_minute": 100, "ai_chat_requests_per_minute": 100}
        )
        self.service = TelegramService(
            configured,
            self.repo,
            context,
            AIOrchestrator(self.gateway, "rules", tools),
            self,
            42,
            "groupbot",
            self.memory,
            self.plans,
        )

    async def send_message(self, chat_id, text, reply_to_message_id=None, parse_mode="HTML"):
        self.message_id += 1
        result = {
            "message_id": self.message_id,
            "date": 1700000000,
            "chat": {"id": chat_id, "type": "supergroup"},
            "from": {"id": 42, "first_name": "Bot", "is_bot": True},
            "text": text,
        }
        self.sent.append(result)
        return result

    async def incoming(self, text, builder=None, user=99):
        self.message_id += 1
        self.update_id += 1
        message = {
            "message_id": self.message_id,
            "date": 1700000000,
            "chat": {"id": -1001, "type": "supergroup", "title": "Group"},
            "from": {"id": user, "first_name": "Jian" if user == 99 else "Anna"},
            "text": "/ask " + text if builder else text,
            "entities": [{"type": "bot_command", "offset": 0, "length": 4}] if builder else [],
        }
        update = {"update_id": self.update_id, "message": message}
        if builder:
            self.gateway.steps = [builder, "Completed."]
        await self.service.process(update)
        assert not self.gateway.steps
        return update


def upgrade(connection):
    for name in (
        "0001_phase1.py",
        "0002_memory.py",
        "0003_plans.py",
        "0004_plan_artifacts.py",
    ):
        spec = spec_from_file_location(name, Path(__file__).parents[1] / "alembic/versions" / name)
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()


@pytest_asyncio.fixture
async def pipeline(settings):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for the plan pipeline")
    database, schema = Database(url), "plan_pipeline_" + uuid4().hex
    async with database.engine.begin() as connection:
        await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    database.engine.update_execution_options(schema_translate_map={None: schema})
    try:
        async with database.engine.begin() as connection:
            await connection.run_sync(upgrade)
        yield Pipeline(settings, database)
    finally:
        async with database.engine.begin() as connection:
            await connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        await database.close()


def plan_id(prompt):
    return prompt["active_plan"]["id"]


async def test_plan_lifecycle_context_tools_and_commands(pipeline):
    passive = await pipeline.incoming("For Seoul Trip, hotel must be under PHP 8,000")
    assert pipeline.gateway.calls == 0 and pipeline.sent == []

    def create(prompt):
        assert prompt["current_local_date"] >= "2026-01-01"
        return [
            ToolCall(
                "create_plan",
                {
                    "name": "Seoul Trip",
                    "plan_type": "trip",
                    "start_date": "2027-01-02",
                    "end_date": "2027-01-05",
                    "source_quote": "start a Seoul plan",
                },
            )
        ]

    await pipeline.incoming("start a Seoul plan Jan 2-5", create)
    created_id = pipeline.gateway.results[-1].result["entity"]["id"]

    def constraint(prompt):
        source = next(x for x in prompt["recent_group_context"] if "hotel must" in x["text"])
        return [
            ToolCall(
                "create_plan_item",
                {
                    "plan_id": plan_id(prompt),
                    "item_type": "constraint",
                    "title": "Hotel under PHP 8,000",
                    "status": "active",
                    "source_message_id": source["source_message_id"],
                    "source_quote": "hotel must be under PHP 8,000",
                },
            )
        ]

    await pipeline.incoming("What constraints apply to Seoul Trip?", constraint)
    await pipeline.incoming("Anna is joining Seoul Trip", user=101)

    def decision(prompt):
        return [
            ToolCall(
                "create_plan_item",
                {
                    "plan_id": plan_id(prompt),
                    "item_type": "decision",
                    "title": "Stay in Hongdae",
                    "status": "confirmed",
                    "metadata": {"selected": "Hongdae", "reason": "better nightlife"},
                    "source_quote": "lock in Hongdae because it has better nightlife",
                },
            )
        ]

    await pipeline.incoming("lock in Hongdae because it has better nightlife", decision)

    def task(prompt):
        anna = next(x for x in prompt["observed_members"] if x["name"] == "Anna")
        return [
            ToolCall(
                "create_plan_item",
                {
                    "plan_id": plan_id(prompt),
                    "item_type": "task",
                    "title": "Book hotel",
                    "assigned_user_id": anna["id"],
                    "source_quote": "assign Anna to book the hotel",
                },
            )
        ]

    await pipeline.incoming("assign Anna to book the hotel for Seoul Trip", task)
    task_id = pipeline.gateway.results[-1].result["entity"]["id"]
    await pipeline.incoming(
        "mark the hotel task done for Seoul Trip",
        lambda prompt: [
            ToolCall(
                "update_plan_item",
                {
                    "item_id": task_id,
                    "status": "done",
                    "source_quote": "mark the hotel task done",
                },
            )
        ],
    )
    await pipeline.incoming(
        "add airport transfer as an open question for Seoul Trip",
        lambda prompt: [
            ToolCall(
                "create_plan_item",
                {
                    "plan_id": plan_id(prompt),
                    "item_type": "open_question",
                    "title": "Airport transfer",
                    "source_quote": "add airport transfer as an open question",
                },
            )
        ],
    )
    question_id = pipeline.gateway.results[-1].result["entity"]["id"]
    await pipeline.incoming(
        "mark airport transfer resolved for Seoul Trip",
        lambda prompt: [
            ToolCall(
                "update_plan_item",
                {
                    "item_id": question_id,
                    "status": "resolved",
                    "source_quote": "mark airport transfer resolved",
                },
            )
        ],
    )
    calls = pipeline.gateway.calls
    await pipeline.incoming("/plans")
    await pipeline.incoming("/plan Seoul Trip")
    assert pipeline.gateway.calls == calls
    assert any("Seoul Trip" in sent["text"] for sent in pipeline.sent[-2:])
    await pipeline.service.process(passive)
    assert pipeline.gateway.calls == calls

    async with pipeline.database.sessions() as session:
        plan = await session.get(Plan, created_id)
        items = list(
            (await session.execute(select(PlanItem).where(PlanItem.plan_id == plan.id))).scalars()
        )
        assert (plan.start_date.isoformat(), plan.end_date.isoformat()) == (
            "2027-01-02",
            "2027-01-05",
        )
        assert {x.item_type for x in items} == {"constraint", "decision", "task", "open_question"}
        assert next(x for x in items if x.item_type == "task").status == "done"
        assert next(x for x in items if x.item_type == "open_question").status == "resolved"
        assert (
            next(x for x in items if x.item_type == "decision").metadata_json["reason"]
            == "better nightlife"
        )


async def test_ambiguous_mutation_does_not_write_and_replay_is_idempotent(pipeline):
    def create(name):
        return lambda prompt: [
            ToolCall(
                "create_plan",
                {
                    "name": name,
                    "plan_type": "trip",
                    "source_quote": f"start {name} plan",
                },
            )
        ]

    first = await pipeline.incoming("start Seoul Trip plan", create("Seoul Trip"))
    await pipeline.incoming("start Tokyo Trip plan", create("Tokyo Trip"))

    def ambiguous(prompt):
        assert prompt["active_plan"] is None and len(prompt["available_plans"]) == 2
        return [
            ToolCall(
                "create_plan_item",
                {
                    "plan_id": prompt["available_plans"][0]["id"],
                    "item_type": "task",
                    "title": "Book hotel",
                    "source_quote": "add task to Seoul Trip or Tokyo Trip",
                },
            )
        ]

    await pipeline.incoming("add task to Seoul Trip or Tokyo Trip", ambiguous)
    assert pipeline.gateway.results[-1].result == {"ok": False, "error": "ambiguous_plan"}
    calls, sent = pipeline.gateway.calls, len(pipeline.sent)
    passive = await pipeline.incoming("Maybe visit a museum")
    await pipeline.service.process(passive)
    await pipeline.service.process(first)
    assert (pipeline.gateway.calls, len(pipeline.sent)) == (calls, sent)
    async with pipeline.database.sessions() as session:
        assert (await session.execute(select(func.count()).select_from(PlanItem))).scalar_one() == 0
        assert (await session.execute(select(func.count()).select_from(AIRun))).scalar_one() == 3
        assert (
            await session.execute(select(func.count()).select_from(UpdateProcessing))
        ).scalar_one() == 4
