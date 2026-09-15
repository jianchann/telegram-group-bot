"""Strict plan tools with current-chat ownership and conversational evidence."""

import json
import re
import unicodedata
from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.schemas.ai import AssistantContext, ToolCall, ToolDeclaration, ToolResult
from app.schemas.plan import PlanDomainError
from app.services.plan_service import PlanService
from app.services.research_router import extract_urls


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class ListArgs(Arguments):
    status: str | None = "active"


class GetArgs(Arguments):
    plan_id: UUID


class CreateArgs(Arguments):
    name: str = Field(min_length=1, max_length=200)
    plan_type: str | None = Field(default=None, max_length=50)
    description: str | None = Field(default=None, max_length=2000)
    start_date: date | None = None
    end_date: date | None = None
    participant_ids: list[UUID] = Field(default_factory=list, max_length=50)
    source_quote: str = Field(min_length=1, max_length=2000)


class UpdatePlanArgs(Arguments):
    plan_id: UUID
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    status: Literal["draft", "active", "completed", "cancelled", "archived"] | None = None
    start_date: date | None = None
    end_date: date | None = None
    source_quote: str = Field(min_length=1, max_length=2000)


class MemberArgs(Arguments):
    plan_id: UUID
    user_id: UUID
    role: str | None = Field(default=None, max_length=100)
    status: Literal["active", "removed"] = "active"
    source_quote: str = Field(min_length=1, max_length=2000)


class CreateItemArgs(Arguments):
    plan_id: UUID
    item_type: Literal["task", "decision", "constraint", "activity", "open_question", "note"]
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    status: str | None = Field(default=None, max_length=50)
    assigned_user_id: UUID | None = None
    due_at: datetime | None = None
    category: str | None = Field(default=None, max_length=100)
    metadata: dict[str, Any] = Field(default_factory=dict)
    source_message_id: UUID | None = None
    source_quote: str = Field(min_length=1, max_length=2000)


class UpdateItemArgs(Arguments):
    item_id: UUID
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    status: str | None = Field(default=None, max_length=50)
    assigned_user_id: UUID | None = None
    due_at: datetime | None = None
    category: str | None = Field(default=None, max_length=100)
    metadata_patch: dict[str, Any] | None = None
    source_quote: str = Field(min_length=1, max_length=2000)


class DeleteItemArgs(Arguments):
    item_id: UUID
    source_quote: str = Field(min_length=1, max_length=2000)


class SaveArtifactArgs(Arguments):
    plan_id: UUID
    url: str = Field(min_length=1, max_length=2048)
    title: str | None = Field(default=None, max_length=300)
    category: str | None = Field(default=None, max_length=100)
    metadata: dict[str, Any] = Field(default_factory=dict)
    source_message_id: UUID | None = None
    source_quote: str = Field(min_length=1, max_length=2000)


class ListArtifactArgs(Arguments):
    plan_id: UUID
    category: str | None = Field(default=None, max_length=100)


def _normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _unquoted(text: str) -> str:
    text = re.sub(r"```.*?```|`[^`]*`|\"[^\"]*\"", " ", text, flags=re.S)
    text = re.sub(r"(?<!\w)'[^'\n]+'(?!\w)", " ", text)
    return " ".join(line for line in text.splitlines() if not line.lstrip().startswith(">"))


def _direct(text: str) -> str:
    return _normalized(re.sub(r"@\w+", " ", _unquoted(text))).strip(" ,.!?")


def _intent(text: str, action: str) -> bool:
    value = _direct(text)
    if re.search(
        r"\b(?:do not|don't|never|without)\b.{0,60}\b(?:create|start|update|change|"
        r"add|assign|mark|delete|remove|drop|cancel|archive|complete|save|keep|"
        r"attach|record|bookmark|store)\b",
        value,
    ):
        return False
    patterns = {
        "create": r"\b(?:create|start|make|set up)\b.{0,30}\b(?:plan|trip|event|project)\b",
        "update_plan": r"(?:\b(?:update|rename|change|cancel|archive|complete|reopen)\b"
        r".{0,50}\b(?:plan|trip|event|project)\b|\b(?:move|reschedule)\b)",
        "member": r"\b(?:add|remove|include|assign)\b.{0,50}\b"
        r"(?:member|participant|traveler|team|to the plan|from the plan)\b",
        "item": r"\b(?:add|save|record|create|assign|mark|update|change|move|lock|"
        r"confirm|decide|will handle|increase)\b",
        "delete": r"\b(?:delete|remove|drop)\b",
        "save_artifact": r"\b(?:save|add|keep|attach|record)\b.{0,80}\b"
        r"(?:link|url|website|page|that|this|result|option|hotel|restaurant)\b",
    }
    return bool(re.search(patterns[action], value))


class PlanToolExecutor:
    def __init__(self, context: AssistantContext, service: PlanService) -> None:
        self.context = context
        self.service = service
        self.actions: list[dict[str, Any]] = []
        self.known_plans: dict[UUID, Any] = {}
        self.known_items: dict[UUID, Any] = {}
        self.mutation_plan_ids: set[UUID] = set()
        plans = [context.active_plan] if context.active_plan else []
        plans.extend(getattr(context, "plan_candidates", []))
        for plan in plans:
            plan_id = getattr(plan, "id", None)
            items = getattr(plan, "items", [])
            if isinstance(plan, dict):
                plan_id, items = plan.get("id"), plan.get("items", [])
            try:
                self.known_plans[UUID(str(plan_id))] = plan
                if plan is context.active_plan:
                    self.mutation_plan_ids.add(UUID(str(plan_id)))
                for item in items:
                    item_id = item.get("id") if isinstance(item, dict) else item.id
                    self.known_items[UUID(str(item_id))] = item
            except (ValueError, TypeError, KeyError):
                continue
        declarations: list[tuple[str, str, type[Arguments]]] = [
            ("list_plans", "List plans owned by this chat.", ListArgs),
            ("get_plan", "Read one known plan in this chat.", GetArgs),
            (
                "create_plan",
                "Create a plan only when the current user directly requests it.",
                CreateArgs,
            ),
            (
                "update_plan",
                "Update a known plan only with direct current intent; clarify ambiguity.",
                UpdatePlanArgs,
            ),
            (
                "set_plan_member",
                "Add, change, or remove an observed member with direct intent.",
                MemberArgs,
            ),
            (
                "create_plan_item",
                "Create an item. Automatic capture is limited to exact human evidence for "
                "constraints or confirmed decisions on an unambiguous plan.",
                CreateItemArgs,
            ),
            (
                "update_plan_item",
                "Update a known item only with direct current intent.",
                UpdateItemArgs,
            ),
            (
                "delete_plan_item",
                "Delete a known item only with direct current destructive intent.",
                DeleteItemArgs,
            ),
            (
                "save_plan_artifact",
                "Save a URL to the resolved plan only on direct current user intent. The URL must "
                "come from the current/replied human message or verified grounding metadata from "
                "this invocation.",
                SaveArtifactArgs,
            ),
            (
                "list_plan_artifacts",
                "List saved URLs for a known plan in this chat, optionally by category.",
                ListArtifactArgs,
            ),
        ]
        self.declarations = [
            ToolDeclaration(name, description, model.model_json_schema())
            for name, description, model in declarations
        ]

    def _identity(self) -> tuple[UUID, UUID]:
        if self.context.current_user_id is None or self.context.current_message_id is None:
            raise PlanDomainError("identity_required")
        return self.context.current_user_id, self.context.current_message_id

    def _plan(self, plan_id: UUID) -> None:
        if plan_id not in self.mutation_plan_ids:
            raise PlanDomainError("ambiguous_plan")

    def _item(self, item_id: UUID) -> Any:
        if item_id not in self.known_items:
            raise PlanDomainError("ambiguous_plan_item")
        if len(self.known_items) == 1:
            return self.known_items[item_id]
        request_words = set(re.findall(r"\w+", _direct(self.context.current_message))) - {
            "the",
            "a",
            "an",
            "that",
            "this",
            "it",
            "please",
            "delete",
            "remove",
            "drop",
            "update",
            "change",
            "mark",
            "move",
        }
        scores: dict[UUID, int] = {}
        for known_id, item in self.known_items.items():
            value = item if isinstance(item, dict) else item.as_dict()
            haystack = " ".join(
                str(value.get(key) or "")
                for key in ("title", "description", "item_type", "assigned_name", "category")
            )
            scores[known_id] = len(request_words & set(re.findall(r"\w+", _normalized(haystack))))
        best = max(scores.values(), default=0)
        matches = [known_id for known_id, score in scores.items() if best > 0 and score == best]
        if matches != [item_id]:
            raise PlanDomainError("ambiguous_plan_item")
        return self.known_items[item_id]

    def _member(self, user_id: UUID) -> None:
        if not any(member.id == user_id for member in self.context.members):
            raise PlanDomainError("unknown_member")

    def _source(self, source_id: UUID | None, quote: str) -> Any:
        source_id = source_id or self.context.current_message_id
        source = next((source for source in self.context.sources if source.id == source_id), None)
        if (
            source is None
            or source.is_bot_message
            or not quote.strip()
            or _normalized(quote) not in _normalized(_unquoted(source.text))
        ):
            raise PlanDomainError("invalid_conversational_evidence")
        return source

    def _decision_confirmed(self, source: Any) -> bool:
        return any(
            not entry.is_bot_message
            and (
                entry.id == source.id
                or entry.reply_to_telegram_message_id == source.telegram_message_id
            )
            and not re.search(r"\b(?:not|don't|never|maybe|perhaps)\b", _normalized(entry.text))
            and re.search(
                r"\b(?:agreed|confirmed|decided|lock .{0,20}in|"
                r"we (?:choose|chose)|we're going with)\b",
                _normalized(_unquoted(entry.text)),
            )
            for entry in self.context.sources
        )

    def _remember(self, mutation: Any) -> dict[str, Any]:
        entity = getattr(mutation, "member", None)
        kind = "plan_member"
        if entity is None:
            entity = getattr(mutation, "artifact", None)
            kind = "plan_artifact"
        if entity is None:
            entity = mutation.item
            kind = "plan_item"
        if entity is None:
            entity = mutation.plan
            kind = "plan"
        if entity is None:
            raise PlanDomainError("invalid_plan_mutation")
        value = {"kind": kind, "action": mutation.action, "entity": entity.as_dict()}
        self.actions.append(value)
        if kind == "plan":
            self.known_plans[entity.id] = entity
            self.mutation_plan_ids.add(entity.id)
        elif kind == "plan_item":
            self.known_items[entity.id] = entity
        return {"ok": True, **value}

    async def execute(self, call: ToolCall) -> ToolResult:
        try:
            payload = json.dumps(call.arguments)
            args: Any
            if call.name == "list_plans":
                args = ListArgs.model_validate_json(payload)
                plans, total = await self.service.list_page(self.context.chat_id, args.status)
                bounded: list[dict[str, Any]] = []
                for plan in plans:
                    entry = plan.as_dict()
                    if len(json.dumps([*bounded, entry], ensure_ascii=False)) > 8000:
                        break
                    bounded.append(entry)
                    self.known_plans[plan.id] = plan
                result = {
                    "ok": True,
                    "plans": bounded,
                    "total": total,
                    "truncated": len(bounded) < len(plans),
                }
            elif call.name == "get_plan":
                args = GetArgs.model_validate_json(payload)
                if args.plan_id not in self.known_plans:
                    raise PlanDomainError("unknown_plan")
                plan = await self.service.get(self.context.chat_id, args.plan_id)
                self.known_plans[plan.id] = plan
                detail = plan.as_dict()
                truncated = False
                while detail.get("items") and len(json.dumps(detail, ensure_ascii=False)) > 8000:
                    detail["items"].pop()
                    truncated = True
                if plan.id in self.mutation_plan_ids:
                    exposed_ids = {UUID(item["id"]) for item in detail.get("items", [])}
                    for item in plan.items:
                        if item.id in exposed_ids:
                            self.known_items[item.id] = item
                result = {"ok": True, "plan": detail, "truncated": truncated}
            elif call.name == "create_plan":
                args = CreateArgs.model_validate_json(payload)
                actor, source = self._identity()
                if not _intent(self.context.current_message, "create"):
                    raise PlanDomainError("explicit_create_intent_required")
                evidence = self._source(source, args.source_quote)
                if self.context.current_local_date and not re.search(
                    r"\b(?:19|20)\d{2}\b", evidence.text
                ):
                    today = date.fromisoformat(self.context.current_local_date)
                    if (args.end_date or args.start_date) and (
                        args.end_date or args.start_date
                    ) < today:
                        raise PlanDomainError("invalid_date_range")
                for user_id in args.participant_ids:
                    self._member(user_id)
                result = self._remember(
                    await self.service.create(
                        self.context.chat_id,
                        actor,
                        source,
                        args.name,
                        plan_type=args.plan_type,
                        description=args.description,
                        start_date=args.start_date,
                        end_date=args.end_date,
                        timezone=self.context.chat_timezone,
                        participant_ids=args.participant_ids,
                    )
                )
            elif call.name == "update_plan":
                args = UpdatePlanArgs.model_validate_json(payload)
                actor, source = self._identity()
                self._plan(args.plan_id)
                if not _intent(self.context.current_message, "update_plan"):
                    raise PlanDomainError("explicit_plan_update_intent_required")
                direct_request = _direct(self.context.current_message)
                if args.status is not None:
                    status_intents = {
                        "draft": r"\bdraft\b",
                        "active": r"\b(?:activate|reopen|resume)\b",
                        "completed": r"\b(?:complete|completed|finish|finished)\b",
                        "cancelled": r"\b(?:cancel|cancelled)\b",
                        "archived": r"\b(?:archive|archived)\b",
                    }
                    if not re.search(status_intents[args.status], direct_request):
                        raise PlanDomainError("unsupported_plan_status_change")
                self._source(source, args.source_quote)
                result = self._remember(
                    await self.service.update(
                        self.context.chat_id,
                        actor,
                        source,
                        args.plan_id,
                        name=args.name,
                        description=args.description,
                        status=args.status,
                        start_date=args.start_date,
                        end_date=args.end_date,
                    )
                )
            elif call.name == "set_plan_member":
                args = MemberArgs.model_validate_json(payload)
                actor, source = self._identity()
                self._plan(args.plan_id)
                self._member(args.user_id)
                if not _intent(self.context.current_message, "member"):
                    raise PlanDomainError("explicit_member_intent_required")
                self._source(source, args.source_quote)
                result = self._remember(
                    await self.service.set_member(
                        self.context.chat_id,
                        actor,
                        source,
                        args.plan_id,
                        args.user_id,
                        role=args.role,
                        status=args.status,
                    )
                )
            elif call.name == "create_plan_item":
                args = CreateItemArgs.model_validate_json(payload)
                actor, current_source = self._identity()
                self._plan(args.plan_id)
                source = self._source(args.source_message_id, args.source_quote)
                direct = _intent(self.context.current_message, "item")
                if direct and source.id != current_source:
                    raise PlanDomainError("explicit_intent_requires_current_evidence")
                if args.assigned_user_id is not None:
                    self._member(args.assigned_user_id)
                    if not direct:
                        raise PlanDomainError("explicit_assignment_intent_required")
                if not direct:
                    if args.item_type not in {"constraint", "decision"}:
                        raise PlanDomainError("explicit_item_intent_required")
                    if source.id == current_source and re.search(
                        r"\b(?:do not|don't|never)\b.{0,30}\b(?:save|record|add)\b",
                        _direct(self.context.current_message),
                    ):
                        raise PlanDomainError("explicit_item_intent_required")
                    if re.search(
                        r"\b(?:maybe|perhaps|might|probably|tentative|idea)\b",
                        _normalized(source.text),
                    ):
                        raise PlanDomainError("unconfirmed_plan_fact")
                    if args.item_type == "decision":
                        if args.status != "confirmed":
                            raise PlanDomainError("decision_not_confirmed")
                        if not self._decision_confirmed(source):
                            raise PlanDomainError("decision_not_confirmed")
                if (
                    args.item_type == "decision"
                    and args.status == "confirmed"
                    and not re.search(
                        r"\b(?:lock|confirm|decide|choose|chose|going with)\b",
                        _direct(self.context.current_message),
                    )
                    and not self._decision_confirmed(source)
                ):
                    raise PlanDomainError("decision_not_confirmed")
                result = self._remember(
                    await self.service.add_item(
                        self.context.chat_id,
                        actor,
                        source.id,
                        args.plan_id,
                        item_type=args.item_type,
                        title=args.title,
                        description=args.description,
                        status=args.status,
                        assigned_user_id=args.assigned_user_id,
                        due_at=args.due_at,
                        category=args.category,
                        metadata=args.metadata,
                        created_by_model=not direct,
                    )
                )
            elif call.name == "update_plan_item":
                args = UpdateItemArgs.model_validate_json(payload)
                actor, source = self._identity()
                target = self._item(args.item_id)
                if not _intent(self.context.current_message, "item"):
                    raise PlanDomainError("explicit_item_update_intent_required")
                self._source(source, args.source_quote)
                if args.assigned_user_id is not None:
                    self._member(args.assigned_user_id)
                    if not re.search(
                        r"\b(?:assign|assigned|will handle|responsible)\b",
                        _direct(self.context.current_message),
                    ):
                        raise PlanDomainError("explicit_assignment_intent_required")
                target_type = (
                    target.get("item_type") if isinstance(target, dict) else target.item_type
                )
                if target_type == "decision" and args.status is not None:
                    required = {
                        "confirmed": r"\b(?:lock|confirm|decide|choose|chose|going with)\b",
                        "reversed": r"\b(?:reverse|reconsider|undo)\b",
                        "proposed": r"\b(?:propose|reopen)\b",
                    }
                    pattern = required.get(args.status)
                    if pattern is None or not re.search(
                        pattern, _direct(self.context.current_message)
                    ):
                        raise PlanDomainError("unsupported_decision_status_change")
                result = self._remember(
                    await self.service.update_item(
                        self.context.chat_id,
                        actor,
                        source,
                        args.item_id,
                        title=args.title,
                        description=args.description,
                        status=args.status,
                        assigned_user_id=args.assigned_user_id,
                        due_at=args.due_at,
                        category=args.category,
                        metadata=args.metadata_patch,
                    )
                )
            elif call.name == "delete_plan_item":
                args = DeleteItemArgs.model_validate_json(payload)
                actor, source = self._identity()
                self._item(args.item_id)
                if not _intent(self.context.current_message, "delete"):
                    raise PlanDomainError("explicit_delete_intent_required")
                self._source(source, args.source_quote)
                result = self._remember(
                    await self.service.delete_item(
                        self.context.chat_id, actor, source, args.item_id
                    )
                )
            elif call.name == "save_plan_artifact":
                args = SaveArtifactArgs.model_validate_json(payload)
                actor, authorization_source = self._identity()
                self._plan(args.plan_id)
                if not _intent(self.context.current_message, "save_artifact"):
                    raise PlanDomainError("explicit_artifact_save_intent_required")
                artifact_source_id = args.source_message_id or authorization_source
                research_url = args.url in self.context.verified_research_urls
                allowed_sources = {authorization_source}
                if self.context.reply_message and self.context.reply_message.source_id:
                    allowed_sources.add(self.context.reply_message.source_id)
                if re.search(
                    r"\b(?:that|this|previous|above|link|url)\b",
                    _direct(self.context.current_message),
                ):
                    prior_with_urls = [
                        source
                        for source in self.context.sources
                        if source.id != authorization_source and extract_urls(source.text)
                    ]
                    if prior_with_urls:
                        nearest = max(
                            prior_with_urls, key=lambda source: source.telegram_message_id
                        )
                        if extract_urls(nearest.text) == [args.url]:
                            allowed_sources.add(nearest.id)
                if artifact_source_id not in allowed_sources and not research_url:
                    raise PlanDomainError("artifact_source_must_be_current_or_referenced")
                if research_url:
                    artifact_source = self._source(authorization_source, args.source_quote)
                else:
                    artifact_source = self._source(artifact_source_id, args.source_quote)
                if not research_url and args.url not in _unquoted(artifact_source.text):
                    raise PlanDomainError("artifact_url_not_in_source")
                result = self._remember(
                    await self.service.save_artifact(
                        self.context.chat_id,
                        actor,
                        authorization_source,
                        artifact_source.id,
                        args.plan_id,
                        args.url,
                        title=args.title,
                        category=args.category,
                        metadata=args.metadata,
                    )
                )
            elif call.name == "list_plan_artifacts":
                args = ListArtifactArgs.model_validate_json(payload)
                if args.plan_id not in self.known_plans:
                    raise PlanDomainError("unknown_plan")
                artifacts = await self.service.list_artifacts(
                    self.context.chat_id, args.plan_id, args.category
                )
                result = {
                    "ok": True,
                    "artifacts": [artifact.as_dict() for artifact in artifacts],
                }
            else:
                result = {"ok": False, "error": "unknown_tool"}
        except (ValidationError, TypeError, ValueError):
            result = {"ok": False, "error": "invalid_tool_arguments"}
        except PlanDomainError as error:
            result = {"ok": False, "error": error.code}
        return ToolResult(call.name, result, call.call_id)
