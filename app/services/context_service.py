"""Bounded, chat-scoped conversational context."""

from datetime import datetime
from typing import Any, Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

from app.repositories.bot import AcceptedMessage, MessageRecord
from app.schemas.ai import AssistantContext, ContextMessage
from app.schemas.memory import MemoryDomainError
from app.services.memory_service import MemoryService
from app.services.plan_service import PlanService


class ContextTooLarge(Exception):
    pass


class HistoryRepository(Protocol):
    async def recent_messages(
        self, chat_id: UUID, before_message_id: int, limit: int
    ) -> list[MessageRecord]: ...


def speaker_name(message: dict[str, Any]) -> str:
    sender = message.get("sender_chat") or message.get("from") or {}
    name = " ".join(str(sender.get(key, "")) for key in ("first_name", "last_name")).strip()
    return (name or str(sender.get("title") or sender.get("username") or "Group member"))[:200]


class ContextService:
    def __init__(
        self,
        repository: HistoryRepository,
        recent_limit: int,
        max_chars: int,
        memory_service: MemoryService | None = None,
        max_memory_results: int = 20,
        auto_memory_enabled: bool = True,
        plan_service: PlanService | None = None,
    ) -> None:
        self.repository = repository
        self.recent_limit = recent_limit
        self.max_chars = max_chars if memory_service is None else max_chars // 2
        self.memory_service = memory_service
        self.max_memory_results = max_memory_results
        self.auto_memory_enabled = auto_memory_enabled
        self.plan_service = plan_service

    async def build_context(
        self,
        accepted: AcceptedMessage,
        message: dict[str, Any],
        request: str,
        summary: str | None = None,
        summary_through_telegram_message_id: int | None = None,
    ) -> AssistantContext:
        context = AssistantContext(
            chat_id=accepted.chat_id,
            current_user=speaker_name(message),
            current_message=request,
            current_user_id=accepted.user_id,
            current_message_id=accepted.message_id,
            current_telegram_message_id=accepted.telegram_message_id,
            summary=summary,
        )
        if len(context.prompt()) > self.max_chars:
            raise ContextTooLarge("Current request exceeds the conversational input limit")
        records = await self.repository.recent_messages(
            accepted.chat_id, accepted.telegram_message_id, self.recent_limit
        )
        context.recent_messages = [
            ContextMessage(
                row.telegram_message_id, row.display_name[:200], row.text, row.is_bot_message
            )
            for row in records
            if row.text
            and (
                summary_through_telegram_message_id is None
                or row.telegram_message_id > summary_through_telegram_message_id
            )
        ]
        reply = message.get("reply_to_message")
        if isinstance(reply, dict):
            reply_text = reply.get("text") or reply.get("caption")
            reply_id = reply.get("message_id")
            if (
                isinstance(reply_text, str)
                and type(reply_id) is int
                and reply_id < accepted.telegram_message_id
                and not any(row.message_id == reply_id for row in context.recent_messages)
            ):
                context.reply_message = ContextMessage(
                    reply_id,
                    speaker_name(reply),
                    reply_text,
                    bool((reply.get("from") or {}).get("is_bot")),
                )
        if self.memory_service is not None:
            context.members = await self.memory_service.members(accepted.chat_id)
            settings = await self.memory_service.settings(accepted.chat_id)
            context.auto_memory_enabled = (
                self.auto_memory_enabled and settings.get("auto_memory_enabled", True) is True
            )
            matching = await self.memory_service.search(
                accepted.chat_id, request[:2000], limit=self.max_memory_results
            )
            fallback = await self.memory_service.search(
                accepted.chat_id, limit=self.max_memory_results
            )
            seen = {memory.id for memory in matching}
            context.memories = (
                matching + [memory for memory in fallback if memory.id not in seen]
            )[: self.max_memory_results]
            context.sources = await self.memory_service.sources(
                accepted.chat_id,
                [accepted.telegram_message_id] + [row.telegram_message_id for row in records],
            )
            if context.reply_message is not None:
                try:
                    context.sources.extend(
                        await self.memory_service.sources(
                            accepted.chat_id, [context.reply_message.message_id]
                        )
                    )
                except MemoryDomainError as exc:
                    if exc.code != "source_not_found":
                        raise
            source_map = {source.telegram_message_id: source for source in context.sources}
            for entry in context.recent_messages + (
                [context.reply_message] if context.reply_message else []
            ):
                source = source_map.get(entry.message_id)
                if source:
                    entry_index = (
                        context.recent_messages.index(entry)
                        if entry in context.recent_messages
                        else None
                    )
                    updated = ContextMessage(
                        entry.message_id,
                        entry.speaker,
                        entry.text,
                        source.is_bot_message,
                        source.id,
                        source.user_id,
                    )
                    if entry_index is None:
                        context.reply_message = updated
                    else:
                        context.recent_messages[entry_index] = updated
        if self.plan_service is not None:
            resolution = await self.plan_service.resolve(
                accepted.chat_id,
                request=request,
                reply_text=context.reply_message.text if context.reply_message else None,
                recent_texts=[entry.text for entry in context.recent_messages],
            )
            if resolution.plan is not None:
                context.active_plan = await self.plan_service.get(
                    accepted.chat_id, resolution.plan.id, item_limit=50
                )
            else:
                context.plan_candidates = resolution.candidates[:20]
            context.chat_timezone = await self.plan_service.chat_timezone(accepted.chat_id)
            context.current_local_date = (
                datetime.now(ZoneInfo(context.chat_timezone)).date().isoformat()
            )
        # Keep the current request intact; trim oldest history before the explicit reply.
        while context.recent_messages and len(context.prompt()) > self.max_chars:
            context.recent_messages.pop(0)
        if len(context.prompt()) > self.max_chars:
            context.reply_message = None
        while context.memories and len(context.prompt()) > self.max_chars:
            context.memories.pop()
        while (
            context.active_plan is not None
            and context.active_plan.items
            and len(context.prompt()) > self.max_chars
        ):
            context.active_plan = context.active_plan.model_copy(
                update={"items": context.active_plan.items[:-1]}
            )
        while context.plan_candidates and len(context.prompt()) > self.max_chars:
            context.plan_candidates.pop()
        while len(context.members) > 1 and len(context.prompt()) > self.max_chars:
            removable = next(
                (member for member in reversed(context.members) if member.id != accepted.user_id),
                None,
            )
            if removable is None:
                break
            context.members.remove(removable)
        if len(context.prompt()) > self.max_chars:
            raise ContextTooLarge("Current request and required context exceed the input limit")
        eligible_ids = {
            accepted.telegram_message_id,
            *(entry.message_id for entry in context.recent_messages),
        }
        if context.reply_message:
            eligible_ids.add(context.reply_message.message_id)
        context.sources = [
            source
            for source in context.sources
            if source.telegram_message_id in eligible_ids and not source.is_bot_message
        ]
        return context
