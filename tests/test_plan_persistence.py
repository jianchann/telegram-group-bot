"""Real PostgreSQL tests for structured plan ownership and lifecycle."""

import importlib.util
import os
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.db.models import Plan, PlanArtifact, PlanItem, PlanMember
from app.db.session import Database
from app.repositories.bot import BotRepository
from app.schemas.plan import PlanDomainError
from app.services.plan_service import PlanService

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def upgrades(connection):
    for filename in (
        "0001_phase1.py",
        "0002_memory.py",
        "0003_plans.py",
        "0004_plan_artifacts.py",
    ):
        path = Path(__file__).parents[1] / "alembic/versions" / filename
        spec = importlib.util.spec_from_file_location(filename, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()


def message(mid, chat=-1001, user=10):
    return {
        "message_id": mid,
        "date": 1700000000,
        "chat": {"id": chat, "type": "supergroup"},
        "from": {"id": user, "first_name": f"U{user}"},
        "text": "plan evidence",
    }


@pytest_asyncio.fixture
async def env():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL required")
    db = Database(url)
    schema = "plans_" + uuid4().hex
    async with db.engine.begin() as c:
        await c.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    db.engine.update_execution_options(schema_translate_map={None: schema})
    async with db.engine.begin() as c:
        await c.run_sync(upgrades)
    bot = BotRepository(db)
    a = await bot.accept_message(1, message(1), "Asia/Manila")
    b = await bot.accept_message(2, message(2, user=11), "Asia/Manila")
    foreign = await bot.accept_message(3, message(1, -1002, 12), "Asia/Manila")
    try:
        yield PlanService(db), db, a, b, foreign
    finally:
        async with db.engine.begin() as c:
            await c.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        await db.close()


async def test_create_dedup_members_items_and_provenance(env):
    service, db, a, b, _ = env
    made = await service.create(
        a.chat_id,
        a.user_id,
        a.message_id,
        "Seoul Trip",
        "trip",
        start_date=date(2027, 1, 2),
        end_date=date(2027, 1, 5),
        participant_ids=[b.user_id],
    )
    duplicate = await service.create(a.chat_id, b.user_id, b.message_id, "  SEOUL trip ")
    assert made.action == "created" and duplicate.action == "unchanged"
    assert {member.user_id for member in made.plan.members} == {a.user_id, b.user_id}
    item = await service.add_item(
        a.chat_id,
        b.user_id,
        b.message_id,
        made.plan.id,
        "task",
        "Book hotel",
        assigned_user_id=b.user_id,
        due_at=datetime(2027, 1, 1, tzinfo=UTC),
    )
    exact = await service.add_item(
        a.chat_id, a.user_id, a.message_id, made.plan.id, "task", " BOOK   HOTEL "
    )
    assert item.action == "created" and exact.action == "unchanged"
    updated = await service.update_item(
        a.chat_id, a.user_id, a.message_id, item.item.id, status="done"
    )
    assert updated.item.status == "done"
    detail = await service.get(a.chat_id, made.plan.id)
    assert detail.total_item_count == 1 and detail.items[0].assigned_name == "U11"
    async with db.sessions() as session:
        plan = await session.get(Plan, made.plan.id)
        stored = await session.get(PlanItem, item.item.id)
        assert plan.latest_source_message_id == a.message_id
        assert stored.latest_actor_user_id == a.user_id
        member = await session.get(PlanMember, (made.plan.id, b.user_id))
        assert member.status == "active"


async def test_outstanding_task_list_filters_assignment_status_and_chat(env):
    service, _, a, b, foreign = env
    plan = (await service.create(a.chat_id, a.user_id, a.message_id, "Seoul Trip")).plan
    mine = await service.add_item(
        a.chat_id,
        a.user_id,
        a.message_id,
        plan.id,
        "task",
        "Book KTX",
        assigned_user_id=a.user_id,
        due_at=datetime(2027, 1, 2, tzinfo=UTC),
    )
    await service.add_item(
        a.chat_id,
        b.user_id,
        b.message_id,
        plan.id,
        "task",
        "Reserve hotel",
        assigned_user_id=b.user_id,
    )
    finished = await service.add_item(
        a.chat_id, a.user_id, a.message_id, plan.id, "task", "Buy tickets"
    )
    await service.update_item(a.chat_id, a.user_id, a.message_id, finished.item.id, status="done")
    foreign_plan = (
        await service.create(
            foreign.chat_id, foreign.user_id, foreign.message_id, "Other Chat Plan"
        )
    ).plan
    await service.add_item(
        foreign.chat_id,
        foreign.user_id,
        foreign.message_id,
        foreign_plan.id,
        "task",
        "Private task",
    )

    tasks, total = await service.list_tasks(a.chat_id)
    personal, personal_total = await service.list_tasks(a.chat_id, assigned_user_id=a.user_id)
    assert total == 2 and [task.title for task in tasks] == ["Book KTX", "Reserve hotel"]
    assert personal_total == 1 and personal[0].id == mine.item.id
    assert all(task.plan_name == "Seoul Trip" for task in tasks)


async def test_resolution_precedence_ambiguity_soft_delete_and_isolation(env):
    service, db, a, b, foreign = env
    seoul = (await service.create(a.chat_id, a.user_id, a.message_id, "Seoul Trip")).plan
    tokyo = (await service.create(a.chat_id, a.user_id, a.message_id, "Tokyo Trip")).plan
    assert (await service.resolve(a.chat_id, "update Tokyo Trip", "Seoul Trip")).plan.id == tokyo.id
    assert (await service.resolve(a.chat_id, "update it", "Seoul Trip")).plan.id == seoul.id
    ambiguous = await service.resolve(a.chat_id, "update the trip")
    assert ambiguous.reason == "ambiguous" and len(ambiguous.candidates) == 2
    item = await service.add_item(
        a.chat_id,
        a.user_id,
        a.message_id,
        seoul.id,
        "decision",
        "Stay in Hongdae",
        status="confirmed",
        metadata={"selected": "Hongdae", "reason": "nightlife"},
    )
    deleted = await service.delete_item(a.chat_id, b.user_id, b.message_id, item.item.id)
    assert (
        deleted.action == "deleted"
        and (await service.get(a.chat_id, seoul.id)).total_item_count == 0
    )
    with pytest.raises(PlanDomainError):
        await service.get(foreign.chat_id, seoul.id)
    with pytest.raises(PlanDomainError):
        await service.update(a.chat_id, foreign.user_id, a.message_id, seoul.id, status="completed")
    async with db.sessions() as session:
        assert (await session.get(PlanItem, item.item.id)).deleted_at is not None


async def test_validation_partial_dates_status_metadata_timezone(env):
    service, _, a, _, _ = env
    plan = (
        await service.create(
            a.chat_id,
            a.user_id,
            a.message_id,
            "Dinner",
            start_date=date(2027, 3, 1),
            end_date=date(2027, 3, 2),
        )
    ).plan
    with pytest.raises(PlanDomainError, match="invalid_date_range"):
        await service.update(
            a.chat_id, a.user_id, a.message_id, plan.id, start_date=date(2027, 3, 3)
        )
    with pytest.raises(PlanDomainError, match="invalid_timezone"):
        await service.create(a.chat_id, a.user_id, a.message_id, "Bad zone", timezone="Mars/Base")
    with pytest.raises(PlanDomainError, match="invalid_metadata"):
        await service.create(a.chat_id, a.user_id, a.message_id, "Huge", metadata={"x": "a" * 9000})
    item = await service.add_item(a.chat_id, a.user_id, a.message_id, plan.id, "task", "Call venue")
    with pytest.raises(PlanDomainError, match="invalid_item_status"):
        await service.update_item(
            a.chat_id, a.user_id, a.message_id, item.item.id, status="confirmed"
        )
    assert await service.chat_timezone(a.chat_id) == "Asia/Manila"


async def test_url_artifact_upsert_detail_and_chat_isolation(env):
    service, db, a, b, foreign = env
    plan = (await service.create(a.chat_id, a.user_id, a.message_id, "Seoul Trip")).plan
    created = await service.save_artifact(
        a.chat_id,
        a.user_id,
        a.message_id,
        a.message_id,
        plan.id,
        "HTTPS://Example.COM:443/hotel?q=1#rooms",
        title="Hotel",
        category="stay",
    )
    updated = await service.save_artifact(
        a.chat_id,
        b.user_id,
        b.message_id,
        a.message_id,
        plan.id,
        "https://example.com/hotel?q=1",
        title="Preferred hotel",
    )
    assert created.action == "created" and updated.action == "updated"
    assert updated.artifact.normalized_url == "https://example.com/hotel?q=1"
    detail = await service.get(a.chat_id, plan.id)
    assert detail.total_artifact_count == 1
    assert detail.artifacts[0].title == "Preferred hotel"
    assert len(await service.list_artifacts(a.chat_id, plan.id, "stay")) == 1
    with pytest.raises(PlanDomainError, match="plan_not_found"):
        await service.list_artifacts(foreign.chat_id, plan.id)
    async with db.sessions() as session:
        stored = await session.get(PlanArtifact, updated.artifact.id)
        assert stored.source_message_id == a.message_id
        assert stored.latest_source_message_id == b.message_id
