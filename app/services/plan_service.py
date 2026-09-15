"""Validated plan API and deterministic current-chat resolution."""
# mypy: disable-error-code="no-untyped-def,no-untyped-call"

import json
import re
from collections.abc import Awaitable
from datetime import date, datetime
from typing import Any, TypeVar
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.exc import SQLAlchemyError

from app.db.session import Database
from app.repositories.plans import PlanRepository, normalize
from app.schemas.plan import (
    PlanArtifactEntry,
    PlanDomainError,
    PlanEntry,
    PlanInfrastructureError,
    PlanMutation,
    PlanResolution,
    TaskEntry,
)

T = TypeVar("T")
PLAN_STATUSES = {"draft", "active", "completed", "cancelled", "archived"}
PLAN_TYPES = {"trip", "event", "project", "purchase", "general"}
ITEM_STATUSES = {
    "task": {"open", "in_progress", "done", "cancelled"},
    "decision": {"proposed", "confirmed", "reversed"},
    "constraint": {"active", "removed"},
    "activity": {"idea", "shortlisted", "confirmed", "completed", "cancelled"},
    "open_question": {"open", "resolved"},
    "note": {"active", "removed"},
}
DEFAULT_STATUS = {key: next(iter(values)) for key, values in ITEM_STATUSES.items()}
DEFAULT_STATUS.update(
    {
        "task": "open",
        "decision": "proposed",
        "constraint": "active",
        "activity": "idea",
        "open_question": "open",
        "note": "active",
    }
)


class PlanService:
    def __init__(self, database: Database):
        self.repository = PlanRepository(database)

    async def _run(self, operation: Awaitable[T]) -> T:
        try:
            return await operation
        except SQLAlchemyError:
            raise PlanInfrastructureError() from None

    def _text(self, value: str, code="invalid_text", maximum=2000):
        value = value.strip()
        if not value or len(value) > maximum:
            raise PlanDomainError(code)
        return value

    def _metadata(self, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        try:
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            raise PlanDomainError("invalid_metadata") from None
        if len(encoded.encode("utf-8")) > 8192:
            raise PlanDomainError("invalid_metadata")
        return value

    def _timezone(self, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise PlanDomainError("invalid_timezone") from None
        return value

    async def chat_timezone(self, chat_id: UUID) -> str:
        return await self._run(self.repository.chat_timezone(chat_id))

    async def list_page(
        self, chat_id: UUID, status: str | None = "active", page: int = 1, page_size: int = 20
    ) -> tuple[list[PlanEntry], int]:
        if status is not None and status not in PLAN_STATUSES:
            raise PlanDomainError("invalid_plan_status")
        if page < 1 or not 1 <= page_size <= 100:
            raise PlanDomainError("invalid_page")
        return await self._run(self.repository.list_page(chat_id, status, page, page_size))

    async def list_tasks(
        self,
        chat_id: UUID,
        page: int = 1,
        page_size: int = 20,
        assigned_user_id: UUID | None = None,
    ) -> tuple[list[TaskEntry], int]:
        if page < 1 or not 1 <= page_size <= 100:
            raise PlanDomainError("invalid_page")
        return await self._run(
            self.repository.list_tasks(chat_id, page, page_size, assigned_user_id)
        )

    async def get(
        self, chat_id: UUID, plan_id: UUID, item_limit: int = 50, artifact_limit: int = 20
    ) -> PlanEntry:
        if not 1 <= item_limit <= 100 or not 1 <= artifact_limit <= 100:
            raise PlanDomainError("invalid_limit")
        return await self._run(self.repository.get(chat_id, plan_id, item_limit, artifact_limit))

    def _url(self, value: str) -> tuple[str, str]:
        original = value.strip()
        if not original or len(original) > 2048:
            raise PlanDomainError("invalid_artifact_url")
        try:
            parsed = urlsplit(original)
            port = parsed.port
            host = parsed.hostname
        except ValueError:
            raise PlanDomainError("invalid_artifact_url") from None
        if (
            parsed.scheme.casefold() not in {"http", "https"}
            or not host
            or parsed.username
            or parsed.password
        ):
            raise PlanDomainError("invalid_artifact_url")
        try:
            normalized_host = host.encode("idna").decode("ascii").casefold()
        except UnicodeError:
            raise PlanDomainError("invalid_artifact_url") from None
        if ":" in normalized_host and not normalized_host.startswith("["):
            normalized_host = f"[{normalized_host}]"
        scheme = parsed.scheme.casefold()
        default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
        netloc = normalized_host + (f":{port}" if port is not None and not default_port else "")
        normalized = urlunsplit((scheme, netloc, parsed.path or "/", parsed.query, ""))
        if len(normalized) > 2048:
            raise PlanDomainError("invalid_artifact_url")
        return original, normalized

    async def save_artifact(
        self,
        chat_id: UUID,
        actor_id: UUID,
        authorization_source_id: UUID,
        artifact_source_id: UUID,
        plan_id: UUID,
        url: str,
        title: str | None = None,
        category: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PlanMutation:
        original, normalized = self._url(url)
        clean_title = self._text(title, maximum=300) if title is not None else None
        clean_category = self._text(category, maximum=100) if category is not None else None
        return await self._run(
            self.repository.save_artifact(
                chat_id,
                actor_id,
                authorization_source_id,
                artifact_source_id,
                plan_id,
                original,
                normalized,
                clean_title,
                clean_category,
                self._metadata(metadata),
            )
        )

    async def list_artifacts(
        self, chat_id: UUID, plan_id: UUID, category: str | None = None, limit: int = 50
    ) -> list[PlanArtifactEntry]:
        if not 1 <= limit <= 100:
            raise PlanDomainError("invalid_limit")
        clean_category = self._text(category, maximum=100) if category is not None else None
        return await self._run(
            self.repository.list_artifacts(chat_id, plan_id, clean_category, limit)
        )

    async def resolve(
        self,
        chat_id: UUID,
        request: str,
        reply_text: str | None = None,
        recent_texts: list[str] | None = None,
    ) -> PlanResolution:
        plans, _ = await self.list_page(chat_id, "active", 1, 100)
        if not plans:
            return PlanResolution(plan=None, candidates=[], reason="none")

        def matches(text):
            value = normalize(text)
            found = [
                p
                for p in plans
                if re.search(r"(?<!\w)" + re.escape(normalize(p.name)) + r"(?!\w)", value)
            ]
            return [
                plan
                for plan in found
                if not any(
                    plan.id != other.id
                    and normalize(plan.name) in normalize(other.name)
                    and len(normalize(plan.name)) < len(normalize(other.name))
                    for other in found
                )
            ]

        found = matches(request)
        if len(found) == 1:
            return PlanResolution(plan=found[0], candidates=found, reason="explicit")
        if len(found) > 1:
            return PlanResolution(plan=None, candidates=found, reason="ambiguous")
        found = matches(reply_text or "")
        if len(found) == 1:
            return PlanResolution(plan=found[0], candidates=found, reason="reply")
        if len(found) > 1:
            return PlanResolution(plan=None, candidates=found, reason="ambiguous")
        recent_matches = []
        for text in reversed(recent_texts or []):
            found = matches(text)
            if found:
                recent_matches = found
                break
        if len(recent_matches) == 1:
            return PlanResolution(
                plan=recent_matches[0], candidates=recent_matches, reason="recent"
            )
        if len(recent_matches) > 1:
            return PlanResolution(plan=None, candidates=recent_matches, reason="ambiguous")
        if len(plans) == 1:
            return PlanResolution(plan=plans[0], candidates=plans, reason="sole_active")
        return PlanResolution(plan=None, candidates=plans, reason="ambiguous")

    async def create(
        self,
        chat_id: UUID,
        actor_id: UUID,
        source_id: UUID,
        name: str,
        plan_type: str | None = None,
        description: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        timezone: str | None = None,
        metadata: dict[str, Any] | None = None,
        participant_ids: list[UUID] | None = None,
    ) -> PlanMutation:
        if plan_type is not None and plan_type not in PLAN_TYPES:
            raise PlanDomainError("invalid_plan_type")
        if start_date and end_date and start_date > end_date:
            raise PlanDomainError("invalid_date_range")
        if description is not None and len(description) > 2000:
            raise PlanDomainError("invalid_description")
        return await self._run(
            self.repository.create(
                chat_id,
                actor_id,
                source_id,
                self._text(name, maximum=200),
                plan_type,
                description.strip() if description else None,
                start_date,
                end_date,
                self._timezone(timezone),
                self._metadata(metadata),
                participant_ids or [],
            )
        )

    async def update(
        self,
        chat_id: UUID,
        actor_id: UUID,
        source_id: UUID,
        plan_id: UUID,
        name: str | None = None,
        description: str | None = None,
        status: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        timezone: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PlanMutation:
        if status is not None and status not in PLAN_STATUSES:
            raise PlanDomainError("invalid_plan_status")
        if start_date and end_date and start_date > end_date:
            raise PlanDomainError("invalid_date_range")
        if description is not None and len(description) > 2000:
            raise PlanDomainError("invalid_description")
        values = {
            "name": self._text(name, maximum=200) if name is not None else None,
            "description": description,
            "status": status,
            "start_date": start_date,
            "end_date": end_date,
            "timezone": self._timezone(timezone),
            "metadata_json": self._metadata(metadata),
        }
        return await self._run(
            self.repository.update(chat_id, actor_id, source_id, plan_id, values)
        )

    async def set_member(
        self,
        chat_id: UUID,
        actor_id: UUID,
        source_id: UUID,
        plan_id: UUID,
        user_id: UUID,
        role: str | None = None,
        status: str = "active",
    ) -> PlanMutation:
        if status not in {"active", "removed"}:
            raise PlanDomainError("invalid_member_status")
        return await self._run(
            self.repository.set_member(chat_id, actor_id, source_id, plan_id, user_id, role, status)
        )

    async def add_item(
        self,
        chat_id: UUID,
        actor_id: UUID,
        source_id: UUID,
        plan_id: UUID,
        item_type: str,
        title: str,
        description: str | None = None,
        status: str | None = None,
        assigned_user_id: UUID | None = None,
        due_at: datetime | None = None,
        category: str | None = None,
        position: int | None = None,
        metadata: dict[str, Any] | None = None,
        created_by_model: bool = False,
    ) -> PlanMutation:
        if item_type not in ITEM_STATUSES:
            raise PlanDomainError("invalid_item_type")
        status = status or DEFAULT_STATUS[item_type]
        if status not in ITEM_STATUSES[item_type]:
            raise PlanDomainError("invalid_item_status")
        if description is not None and len(description) > 2000:
            raise PlanDomainError("invalid_description")
        if due_at is not None and due_at.utcoffset() is None:
            raise PlanDomainError("invalid_due_at")
        clean_metadata = self._metadata(metadata) or {}
        if (
            item_type == "decision"
            and status == "confirmed"
            and not str(clean_metadata.get("selected", "")).strip()
        ):
            raise PlanDomainError("confirmed_decision_requires_selected_option")
        values = {
            "item_type": item_type,
            "title": self._text(title, maximum=200),
            "description": description,
            "status": status,
            "assigned_user_id": assigned_user_id,
            "due_at": due_at,
            "category": category,
            "position": position,
            "metadata": clean_metadata,
            "created_by_model": created_by_model,
        }
        return await self._run(
            self.repository.add_item(chat_id, actor_id, source_id, plan_id, values)
        )

    async def update_item(
        self,
        chat_id: UUID,
        actor_id: UUID,
        source_id: UUID,
        item_id: UUID,
        title: str | None = None,
        description: str | None = None,
        status: str | None = None,
        assigned_user_id: UUID | None = None,
        due_at: datetime | None = None,
        category: str | None = None,
        position: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PlanMutation:
        if status is not None and not any(status in values for values in ITEM_STATUSES.values()):
            raise PlanDomainError("invalid_item_status")
        if description is not None and len(description) > 2000:
            raise PlanDomainError("invalid_description")
        if due_at is not None and due_at.utcoffset() is None:
            raise PlanDomainError("invalid_due_at")
        values = {
            "title": self._text(title, maximum=200) if title is not None else None,
            "description": description,
            "status": status,
            "assigned_user_id": assigned_user_id,
            "due_at": due_at,
            "category": category,
            "position": position,
            "metadata_json": self._metadata(metadata),
        }
        return await self._run(
            self.repository.update_item(chat_id, actor_id, source_id, item_id, values)
        )

    async def delete_item(
        self, chat_id: UUID, actor_id: UUID, source_id: UUID, item_id: UUID
    ) -> PlanMutation:
        return await self._run(
            self.repository.update_item(chat_id, actor_id, source_id, item_id, {}, True)
        )
