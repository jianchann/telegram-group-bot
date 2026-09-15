"""Chat-scoped structured plan persistence."""
# mypy: disable-error-code="no-untyped-def,no-untyped-call"

import unicodedata
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chat, ChatMember, Message, Plan, PlanArtifact, PlanItem, PlanMember, User
from app.db.session import Database
from app.schemas.plan import (
    PlanArtifactEntry,
    PlanDomainError,
    PlanEntry,
    PlanItemEntry,
    PlanMemberEntry,
    PlanMutation,
    TaskEntry,
)

ITEM_STATUSES = {
    "task": {"open", "in_progress", "done", "cancelled"},
    "decision": {"proposed", "confirmed", "reversed"},
    "constraint": {"active", "removed"},
    "activity": {"idea", "shortlisted", "confirmed", "completed", "cancelled"},
    "open_question": {"open", "resolved"},
    "note": {"active", "removed"},
}


def normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


class PlanRepository:
    def __init__(self, database: Database):
        self.database = database

    async def _chat(self, session: AsyncSession, chat_id: UUID, lock=False):
        query = select(Chat).where(Chat.id == chat_id)
        chat = (
            await session.execute(query.with_for_update() if lock else query)
        ).scalar_one_or_none()
        if chat is None:
            raise PlanDomainError("chat_not_found")
        return chat

    async def _member(self, session: AsyncSession, chat_id: UUID, user_id: UUID):
        user = (
            await session.execute(
                select(User)
                .join(ChatMember, ChatMember.user_id == User.id)
                .where(
                    ChatMember.chat_id == chat_id,
                    ChatMember.user_id == user_id,
                    ChatMember.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if user is None:
            raise PlanDomainError("member_not_found")
        return user

    async def _validate(self, session, chat_id, actor_id, source_id, lock=True):
        await self._chat(session, chat_id, lock)
        await self._member(session, chat_id, actor_id)
        source = (
            await session.execute(
                select(Message).where(
                    Message.id == source_id,
                    Message.chat_id == chat_id,
                    Message.is_bot_message.is_(False),
                )
            )
        ).scalar_one_or_none()
        if source is None or bool(((source.raw_payload or {}).get("from") or {}).get("is_bot")):
            raise PlanDomainError("source_not_found")

    async def _plan(self, session, chat_id, plan_id, lock=False):
        query = select(Plan).where(Plan.chat_id == chat_id, Plan.id == plan_id)
        plan = (
            await session.execute(query.with_for_update() if lock else query)
        ).scalar_one_or_none()
        if plan is None:
            raise PlanDomainError("plan_not_found")
        return plan

    async def _entry(self, session, plan, detail=False, item_limit=50, artifact_limit=20):
        members = []
        items = []
        artifacts = []
        if detail:
            rows = (
                await session.execute(
                    select(PlanMember, User)
                    .join(User, PlanMember.user_id == User.id)
                    .where(PlanMember.plan_id == plan.id)
                    .order_by(User.display_name, User.id)
                )
            ).all()
            members = [
                PlanMemberEntry(
                    user_id=m.user_id,
                    display_name=u.display_name or "Unknown member",
                    role=m.role,
                    status=m.status,
                )
                for m, u in rows
            ]
            total_item_count = (
                await session.execute(
                    select(func.count())
                    .select_from(PlanItem)
                    .where(PlanItem.plan_id == plan.id, PlanItem.deleted_at.is_(None))
                )
            ).scalar_one()
            item_rows = (
                await session.execute(
                    select(PlanItem, User.display_name)
                    .outerjoin(User, PlanItem.assigned_user_id == User.id)
                    .where(PlanItem.plan_id == plan.id, PlanItem.deleted_at.is_(None))
                    .order_by(PlanItem.position.asc().nullslast(), PlanItem.created_at, PlanItem.id)
                    .limit(item_limit)
                )
            ).all()
            items = [
                PlanItemEntry(
                    id=i.id,
                    plan_id=i.plan_id,
                    item_type=i.item_type,
                    title=i.title,
                    description=i.description,
                    status=i.status,
                    assigned_user_id=i.assigned_user_id,
                    assigned_name=name,
                    due_at=i.due_at,
                    category=i.category,
                    position=i.position,
                    metadata=i.metadata_json,
                    source_message_id=i.source_message_id,
                    created_at=i.created_at,
                    updated_at=i.updated_at,
                )
                for i, name in item_rows
            ]
            total_artifact_count = (
                await session.execute(
                    select(func.count())
                    .select_from(PlanArtifact)
                    .where(PlanArtifact.plan_id == plan.id)
                )
            ).scalar_one()
            artifact_rows = (
                (
                    await session.execute(
                        select(PlanArtifact)
                        .where(PlanArtifact.plan_id == plan.id)
                        .order_by(PlanArtifact.category.asc().nullslast(), PlanArtifact.created_at)
                        .limit(artifact_limit)
                    )
                )
                .scalars()
                .all()
            )
            artifacts = [self._artifact_entry(artifact) for artifact in artifact_rows]
        return PlanEntry(
            id=plan.id,
            chat_id=plan.chat_id,
            name=plan.name,
            description=plan.description if detail else None,
            plan_type=plan.plan_type,
            status=plan.status,
            start_date=plan.start_date,
            end_date=plan.end_date,
            timezone=plan.timezone,
            metadata=plan.metadata_json if detail else {},
            created_by_user_id=plan.created_by_user_id,
            members=members,
            items=items,
            artifacts=artifacts,
            total_item_count=total_item_count if detail else 0,
            total_artifact_count=total_artifact_count if detail else 0,
        )

    async def chat_timezone(self, chat_id):
        async with self.database.sessions() as session:
            return (await self._chat(session, chat_id)).timezone

    async def list_page(self, chat_id, status, page, page_size):
        async with self.database.sessions() as session:
            await self._chat(session, chat_id)
            predicates = [Plan.chat_id == chat_id]
            if status is not None:
                predicates.append(Plan.status == status)
            total = (
                await session.execute(select(func.count()).select_from(Plan).where(*predicates))
            ).scalar_one()
            rows = (
                (
                    await session.execute(
                        select(Plan)
                        .where(*predicates)
                        .order_by(Plan.updated_at.desc(), Plan.id)
                        .offset((page - 1) * page_size)
                        .limit(page_size)
                    )
                )
                .scalars()
                .all()
            )
            return [await self._entry(session, plan) for plan in rows], total

    async def list_tasks(self, chat_id, page, page_size, assigned_user_id=None):
        async with self.database.sessions() as session:
            await self._chat(session, chat_id)
            if assigned_user_id is not None:
                await self._member(session, chat_id, assigned_user_id)
            predicates = [
                Plan.chat_id == chat_id,
                Plan.status == "active",
                PlanItem.item_type == "task",
                PlanItem.status.in_(("open", "in_progress")),
                PlanItem.deleted_at.is_(None),
            ]
            if assigned_user_id is not None:
                predicates.append(PlanItem.assigned_user_id == assigned_user_id)
            total = (
                await session.execute(
                    select(func.count())
                    .select_from(PlanItem)
                    .join(Plan, Plan.id == PlanItem.plan_id)
                    .where(*predicates)
                )
            ).scalar_one()
            rows = (
                await session.execute(
                    select(PlanItem, Plan.name, User.display_name)
                    .join(Plan, Plan.id == PlanItem.plan_id)
                    .outerjoin(User, User.id == PlanItem.assigned_user_id)
                    .where(*predicates)
                    .order_by(
                        PlanItem.due_at.asc().nullslast(),
                        PlanItem.created_at,
                        PlanItem.id,
                    )
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            ).all()
            return [
                TaskEntry(
                    id=item.id,
                    plan_id=item.plan_id,
                    plan_name=plan_name,
                    title=item.title,
                    status=item.status,
                    assigned_user_id=item.assigned_user_id,
                    assigned_name=assigned_name,
                    due_at=item.due_at,
                )
                for item, plan_name, assigned_name in rows
            ], total

    async def get(self, chat_id, plan_id, item_limit=50, artifact_limit=20):
        async with self.database.sessions() as session:
            await self._chat(session, chat_id)
            return await self._entry(
                session,
                await self._plan(session, chat_id, plan_id),
                True,
                item_limit,
                artifact_limit,
            )

    @staticmethod
    def _artifact_entry(artifact):
        return PlanArtifactEntry(
            id=artifact.id,
            plan_id=artifact.plan_id,
            artifact_type="url",
            title=artifact.title,
            url=artifact.url,
            normalized_url=artifact.normalized_url,
            category=artifact.category,
            metadata=artifact.metadata_json,
            source_message_id=artifact.source_message_id,
            created_by_user_id=artifact.created_by_user_id,
            latest_actor_user_id=artifact.latest_actor_user_id,
            created_at=artifact.created_at,
            updated_at=artifact.updated_at,
        )

    async def save_artifact(
        self,
        chat_id,
        actor_id,
        authorization_source_id,
        artifact_source_id,
        plan_id,
        url,
        normalized_url,
        title=None,
        category=None,
        metadata=None,
    ):
        async with self.database.sessions.begin() as session:
            await self._validate(session, chat_id, actor_id, authorization_source_id)
            plan = await self._plan(session, chat_id, plan_id, True)
            artifact_source = (
                await session.execute(
                    select(Message).where(
                        Message.id == artifact_source_id,
                        Message.chat_id == chat_id,
                        Message.is_bot_message.is_(False),
                    )
                )
            ).scalar_one_or_none()
            if artifact_source is None:
                raise PlanDomainError("source_not_found")
            now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
            existing = (
                await session.execute(
                    select(PlanArtifact)
                    .where(
                        PlanArtifact.plan_id == plan_id,
                        PlanArtifact.normalized_url == normalized_url,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if existing is None:
                artifact = PlanArtifact(
                    plan_id=plan_id,
                    artifact_type="url",
                    title=title,
                    url=url,
                    normalized_url=normalized_url,
                    category=category,
                    metadata_json=metadata or {},
                    source_message_id=artifact_source_id,
                    created_by_user_id=actor_id,
                    latest_actor_user_id=actor_id,
                    latest_source_message_id=authorization_source_id,
                )
                session.add(artifact)
                action: Literal["created", "updated", "unchanged"] = "created"
            else:
                artifact = existing
                changed = False
                for field, value in (("title", title), ("category", category)):
                    if value is not None and getattr(artifact, field) != value:
                        setattr(artifact, field, value)
                        changed = True
                if metadata is not None and artifact.metadata_json != metadata:
                    artifact.metadata_json = metadata
                    changed = True
                artifact.url = url
                artifact.latest_actor_user_id = actor_id
                artifact.latest_source_message_id = authorization_source_id
                artifact.updated_at = now
                action = "updated" if changed else "unchanged"
            plan.latest_actor_user_id = actor_id
            plan.latest_source_message_id = authorization_source_id
            plan.updated_at = now
            await session.flush()
            return PlanMutation(action=action, artifact=self._artifact_entry(artifact))

    async def list_artifacts(self, chat_id, plan_id, category=None, limit=50):
        async with self.database.sessions() as session:
            await self._chat(session, chat_id)
            await self._plan(session, chat_id, plan_id)
            predicates = [PlanArtifact.plan_id == plan_id]
            if category is not None:
                predicates.append(PlanArtifact.category == category)
            rows = (
                (
                    await session.execute(
                        select(PlanArtifact)
                        .where(*predicates)
                        .order_by(PlanArtifact.category.asc().nullslast(), PlanArtifact.created_at)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            return [self._artifact_entry(artifact) for artifact in rows]

    async def create(
        self,
        chat_id,
        actor_id,
        source_id,
        name,
        plan_type=None,
        description=None,
        start_date=None,
        end_date=None,
        timezone=None,
        metadata=None,
        participant_ids=(),
    ):
        async with self.database.sessions.begin() as session:
            await self._validate(session, chat_id, actor_id, source_id)
            normalized = normalize(name)
            existing = (
                await session.execute(
                    select(Plan).where(
                        Plan.chat_id == chat_id,
                        Plan.normalized_name == normalized,
                        Plan.status.in_(("draft", "active")),
                    )
                )
            ).scalar_one_or_none()
            if existing:
                return PlanMutation(
                    action="unchanged", plan=await self._entry(session, existing, True)
                )
            for user_id in set(participant_ids) | {actor_id}:
                await self._member(session, chat_id, user_id)
            plan = Plan(
                chat_id=chat_id,
                name=name,
                normalized_name=normalized,
                plan_type=plan_type,
                description=description,
                start_date=start_date,
                end_date=end_date,
                timezone=timezone,
                metadata_json=metadata or {},
                status="active",
                created_by_user_id=actor_id,
                latest_actor_user_id=actor_id,
                latest_source_message_id=source_id,
            )
            session.add(plan)
            await session.flush()
            for user_id in set(participant_ids) | {actor_id}:
                session.add(
                    PlanMember(
                        plan_id=plan.id,
                        user_id=user_id,
                        status="active",
                        latest_actor_user_id=actor_id,
                        latest_source_message_id=source_id,
                    )
                )
            await session.flush()
            return PlanMutation(action="created", plan=await self._entry(session, plan, True))

    async def update(self, chat_id, actor_id, source_id, plan_id, values):
        async with self.database.sessions.begin() as session:
            await self._validate(session, chat_id, actor_id, source_id)
            plan = await self._plan(session, chat_id, plan_id, True)
            changed = False
            for field, value in values.items():
                if value is not None and getattr(plan, field) != value:
                    setattr(plan, field, value)
                    changed = True
                    if field == "name":
                        plan.normalized_name = normalize(value)
            if changed:
                if plan.start_date and plan.end_date and plan.start_date > plan.end_date:
                    raise PlanDomainError("invalid_date_range")
                duplicate = (
                    await session.execute(
                        select(Plan.id).where(
                            Plan.chat_id == chat_id,
                            Plan.normalized_name == plan.normalized_name,
                            Plan.status.in_(("draft", "active")),
                            Plan.id != plan.id,
                        )
                    )
                ).scalar_one_or_none()
                if duplicate:
                    raise PlanDomainError("duplicate_plan")
                plan.latest_actor_user_id = actor_id
                plan.latest_source_message_id = source_id
                plan.updated_at = func.clock_timestamp()
            await session.flush()
            return PlanMutation(
                action="updated" if changed else "unchanged",
                plan=await self._entry(session, plan, True),
            )

    async def set_member(self, chat_id, actor_id, source_id, plan_id, user_id, role, status):
        async with self.database.sessions.begin() as session:
            await self._validate(session, chat_id, actor_id, source_id)
            await self._plan(session, chat_id, plan_id, True)
            await self._member(session, chat_id, user_id)
            now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
            statement = insert(PlanMember).values(
                plan_id=plan_id,
                user_id=user_id,
                role=role,
                status=status,
                latest_actor_user_id=actor_id,
                latest_source_message_id=source_id,
                updated_at=now,
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[PlanMember.plan_id, PlanMember.user_id],
                    set_={
                        "role": role,
                        "status": status,
                        "latest_actor_user_id": actor_id,
                        "latest_source_message_id": source_id,
                        "updated_at": now,
                    },
                )
            )
            plan = await self._plan(session, chat_id, plan_id)
            plan.latest_actor_user_id = actor_id
            plan.latest_source_message_id = source_id
            plan.updated_at = now
            user = await self._member(session, chat_id, user_id)
            member = PlanMemberEntry(
                user_id=user_id,
                display_name=user.display_name or "Unknown member",
                role=role,
                status=status,
            )
            return PlanMutation(
                action="updated",
                member=member,
                plan=await self._entry(session, await self._plan(session, chat_id, plan_id), True),
            )

    async def add_item(self, chat_id, actor_id, source_id, plan_id, values):
        async with self.database.sessions.begin() as session:
            await self._validate(session, chat_id, actor_id, source_id)
            plan = await self._plan(session, chat_id, plan_id, True)
            if values.get("assigned_user_id"):
                await self._member(session, chat_id, values["assigned_user_id"])
                membership = insert(PlanMember).values(
                    plan_id=plan_id,
                    user_id=values["assigned_user_id"],
                    status="active",
                    latest_actor_user_id=actor_id,
                    latest_source_message_id=source_id,
                )
                await session.execute(
                    membership.on_conflict_do_update(
                        index_elements=[PlanMember.plan_id, PlanMember.user_id],
                        set_={
                            "status": "active",
                            "latest_actor_user_id": actor_id,
                            "latest_source_message_id": source_id,
                            "updated_at": func.clock_timestamp(),
                        },
                    )
                )
            normalized = normalize(values["title"])
            existing = (
                await session.execute(
                    select(PlanItem).where(
                        PlanItem.plan_id == plan_id,
                        PlanItem.item_type == values["item_type"],
                        PlanItem.normalized_title == normalized,
                        PlanItem.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if existing:
                return PlanMutation(
                    action="unchanged", item=(await self._item_entry(session, existing))
                )
            item = PlanItem(
                plan_id=plan_id,
                normalized_title=normalized,
                source_message_id=source_id,
                created_by_user_id=actor_id,
                latest_actor_user_id=actor_id,
                latest_source_message_id=source_id,
                created_by_model=values.pop("created_by_model", False),
                metadata_json=values.pop("metadata", {}),
                **values,
            )
            session.add(item)
            plan.latest_actor_user_id = actor_id
            plan.latest_source_message_id = source_id
            plan.updated_at = func.clock_timestamp()
            await session.flush()
            return PlanMutation(action="created", item=await self._item_entry(session, item))

    async def _item_entry(self, session, item):
        name = (
            (
                await session.execute(
                    select(User.display_name).where(User.id == item.assigned_user_id)
                )
            ).scalar_one_or_none()
            if item.assigned_user_id
            else None
        )
        return PlanItemEntry(
            id=item.id,
            plan_id=item.plan_id,
            item_type=item.item_type,
            title=item.title,
            description=item.description,
            status=item.status,
            assigned_user_id=item.assigned_user_id,
            assigned_name=name,
            due_at=item.due_at,
            category=item.category,
            position=item.position,
            metadata=item.metadata_json,
            source_message_id=item.source_message_id,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )

    async def update_item(self, chat_id, actor_id, source_id, item_id, values, delete=False):
        async with self.database.sessions.begin() as session:
            await self._validate(session, chat_id, actor_id, source_id)
            item = (
                await session.execute(
                    select(PlanItem)
                    .join(Plan)
                    .where(Plan.chat_id == chat_id, PlanItem.id == item_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if item is None:
                raise PlanDomainError("plan_item_not_found")
            if item.deleted_at is not None:
                raise PlanDomainError("plan_item_deleted")
            plan = await self._plan(session, chat_id, item.plan_id, True)
            if values.get("assigned_user_id"):
                await self._member(session, chat_id, values["assigned_user_id"])
                membership = insert(PlanMember).values(
                    plan_id=item.plan_id,
                    user_id=values["assigned_user_id"],
                    status="active",
                    latest_actor_user_id=actor_id,
                    latest_source_message_id=source_id,
                )
                await session.execute(
                    membership.on_conflict_do_update(
                        index_elements=[PlanMember.plan_id, PlanMember.user_id],
                        set_={
                            "status": "active",
                            "latest_actor_user_id": actor_id,
                            "latest_source_message_id": source_id,
                            "updated_at": func.clock_timestamp(),
                        },
                    )
                )

            if (
                values.get("status") is not None
                and values["status"] not in ITEM_STATUSES[item.item_type]
            ):
                raise PlanDomainError("invalid_item_status")
            prospective_metadata = {
                **item.metadata_json,
                **(values.get("metadata_json") or {}),
            }
            if (
                item.item_type == "decision"
                and (values.get("status") or item.status) == "confirmed"
                and not str(prospective_metadata.get("selected", "")).strip()
            ):
                raise PlanDomainError("confirmed_decision_requires_selected_option")
            now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
            changed = delete or any(
                value is not None and getattr(item, field) != value
                for field, value in values.items()
            )
            if delete:
                item.deleted_at = now
            else:
                for field, value in values.items():
                    if value is not None:
                        if field == "metadata_json":
                            item.metadata_json = {**item.metadata_json, **value}
                        else:
                            setattr(item, field, value)
                if values.get("title"):
                    item.normalized_title = normalize(values["title"])
            if changed:
                item.latest_actor_user_id = actor_id
                item.latest_source_message_id = source_id
                item.updated_at = now
                plan.latest_actor_user_id = actor_id
                plan.latest_source_message_id = source_id
                plan.updated_at = now
            await session.flush()
            return PlanMutation(
                action="deleted" if delete else ("updated" if changed else "unchanged"),
                item=await self._item_entry(session, item),
            )
