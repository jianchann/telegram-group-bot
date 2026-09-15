"""Persist first, invoke explicitly, and confirm only successful deliveries."""

import asyncio
import logging
from time import perf_counter
from typing import Any, Protocol
from uuid import UUID, uuid4

from app.config import Settings
from app.repositories.bot import AcceptedMessage
from app.schemas.ai import AssistantContext, AssistantResponse, GeminiResult
from app.schemas.media import MediaError
from app.schemas.summary import SummaryConflict
from app.services.context_service import ContextService, ContextTooLarge
from app.services.formatting import render_rich_chunks
from app.services.gemini_gateway import GeminiError
from app.services.invocation import detect_invocation
from app.services.media_service import MediaService
from app.services.memory_renderer import render_memory_page
from app.services.memory_service import MemoryService
from app.services.plan_renderer import (
    render_assistant_response,
    render_plan_candidates,
    render_plan_detail,
    render_plan_page,
    render_task_page,
)
from app.services.summary_service import SummaryService
from app.services.telegram_client import TelegramError, TelegramSendUncertain

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "I can answer using this group's recent conversation. Mention me, reply to one "
    "of my messages, or use /ask followed by your question.\n\n"
    "I stay silent during ordinary conversation. /help shows this message. "
    "Ask me to remember, correct, or forget shared group/member facts. "
    "/memory lists shared memories; /memory_me shows facts about you in this group. "
    "Both support a page number. /plans lists active plans and /plan shows one, "
    "including saved links. /tasks lists outstanding tasks; /tasks mine filters "
    "them to you. I can create native polls and research current facts, places, "
    "supplied URLs, and calculations when useful. Reminders are not available yet."
)
AI_FAILURE_TEXT = (
    "I couldn't complete that request right now. The conversation is still saved, "
    "so you can try again."
)


class ProcessingRepository(Protocol):
    async def accept_message(
        self, update_id: int, message: dict[str, Any], default_timezone: str
    ) -> AcceptedMessage | None: ...

    async def reserve_ai_request(
        self, chat_id: UUID, user_id: UUID, user_limit: int, chat_limit: int
    ) -> bool: ...

    async def finish_update(
        self, update_id: int, status: str, error_code: str | None = None
    ) -> None: ...

    async def save_outgoing(self, chat_id: UUID, message: dict[str, Any]) -> None: ...

    async def record_ai_run(
        self,
        chat_id: UUID,
        trigger_message_id: UUID,
        model: str,
        status: str,
        input_tokens: int | None,
        output_tokens: int | None,
        latency_ms: int,
        error_code: str | None = None,
        used_custom_functions: bool = False,
        used_search: bool = False,
        used_maps: bool = False,
        used_url_context: bool = False,
        used_code_execution: bool = False,
        request_type: str = "chat",
    ) -> None: ...


class ResponseOrchestrator(Protocol):
    async def respond(self, context: AssistantContext) -> AssistantResponse: ...


class SummaryGateway(Protocol):
    async def summarize(
        self,
        *,
        prompt: str,
        system_prompt: str,
        max_output_tokens: int,
        timeout_seconds: float,
    ) -> GeminiResult: ...


class MessageSender(Protocol):
    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        parse_mode: str | None = "HTML",
    ) -> dict[str, Any]: ...


class PlanCommands(Protocol):
    async def list_page(
        self, chat_id: UUID, status: str, page: int, page_size: int = 20
    ) -> tuple[list[Any], int]: ...

    async def resolve(
        self,
        chat_id: UUID,
        request: str,
        reply_text: str | None,
        recent_texts: list[str],
    ) -> Any: ...

    async def get(self, chat_id: UUID, plan_id: UUID, item_limit: int = 50) -> Any: ...

    async def list_tasks(
        self,
        chat_id: UUID,
        page: int = 1,
        page_size: int = 20,
        assigned_user_id: UUID | None = None,
    ) -> tuple[list[Any], int]: ...


def _task_request(value: str) -> tuple[bool, int] | None:
    parts = value.split()
    mine = bool(parts and parts[0].casefold() == "mine")
    if mine:
        parts = parts[1:]
    if len(parts) > 1:
        return None
    if not parts:
        return mine, 1
    page = parts[0]
    if not page.isascii() or not page.isdigit() or len(page) > 8 or int(page) < 1:
        return None
    return mine, int(page)


class TelegramService:
    def __init__(
        self,
        settings: Settings,
        repository: ProcessingRepository,
        context_service: ContextService,
        orchestrator: ResponseOrchestrator,
        telegram: MessageSender,
        bot_id: int,
        bot_username: str,
        memory_service: MemoryService | None = None,
        plan_service: PlanCommands | None = None,
        media_service: MediaService | None = None,
        summary_service: SummaryService | None = None,
        summary_gateway: SummaryGateway | None = None,
        summary_system_prompt: str = "",
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.context_service = context_service
        self.orchestrator = orchestrator
        self.telegram = telegram
        self.bot_id = bot_id
        self.bot_username = bot_username
        self.memory_service = memory_service
        self.plan_service = plan_service
        self.media_service = media_service
        self.summary_service = summary_service
        self.summary_gateway = summary_gateway
        self.summary_system_prompt = summary_system_prompt

    async def process(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return  # Edits do not regenerate answers or overwrite original history.
        chat = message.get("chat") or {}
        telegram_chat_id = chat.get("id")
        if (
            chat.get("type") not in {"group", "supergroup"}
            or type(telegram_chat_id) is not int
            or telegram_chat_id not in self.settings.allowed_telegram_chat_ids
        ):
            return
        update_id: int = update["update_id"]
        accepted = await self.repository.accept_message(
            update_id, message, self.settings.default_chat_timezone
        )
        if accepted is None:
            return
        event = {
            "request_id": str(uuid4()),
            "chat_id": str(accepted.chat_id),
            "update_id": update_id,
        }
        try:
            invocation = detect_invocation(message, self.bot_id, self.bot_username)
            status, error_code = "completed", None
            if invocation is None or not accepted.bot_enabled:
                await self.repository.finish_update(update_id, status)
                return
            if invocation.kind in {"help", "start"}:
                text = HELP_TEXT
            elif invocation.kind in {"memory", "memory_me"}:
                personal = invocation.kind == "memory_me"
                if self.memory_service is None:
                    text = "Memory is not configured yet."
                elif personal and accepted.user_id is None:
                    text = "Please use your personal Telegram identity for /memory_me."
                elif invocation.request and (
                    not invocation.request.isascii()
                    or not invocation.request.isdigit()
                    or len(invocation.request) > 8
                    or int(invocation.request) < 1
                ):
                    text = (
                        f"Use /{invocation.kind} followed by a positive page number, "
                        "or no number for page 1."
                    )
                else:
                    page = int(invocation.request) if invocation.request else 1
                    entries, total = await self.memory_service.list_page(
                        accepted.chat_id, accepted.user_id if personal else None, page
                    )
                    text = render_memory_page(entries, total, page, personal)
            elif invocation.kind == "plans":
                if self.plan_service is None:
                    text = "Plans are not configured yet."
                elif invocation.request and (
                    not invocation.request.isascii()
                    or not invocation.request.isdigit()
                    or len(invocation.request) > 8
                    or int(invocation.request) < 1
                ):
                    text = "Use /plans followed by a positive page number, or no number for page 1."
                else:
                    page = int(invocation.request) if invocation.request else 1
                    plans, total = await self.plan_service.list_page(
                        accepted.chat_id, status="active", page=page, page_size=20
                    )
                    text = render_plan_page(plans, total, page)
            elif invocation.kind == "plan":
                if self.plan_service is None:
                    text = "Plans are not configured yet."
                else:
                    resolution = await self.plan_service.resolve(
                        accepted.chat_id,
                        request=invocation.request,
                        reply_text=None,
                        recent_texts=[],
                    )
                    if resolution.plan is not None:
                        detail = await self.plan_service.get(
                            accepted.chat_id, resolution.plan.id, item_limit=50
                        )
                        text = render_plan_detail(detail)
                    else:
                        text = render_plan_candidates(resolution.candidates)
            elif invocation.kind == "tasks":
                parsed = _task_request(invocation.request)
                if self.plan_service is None:
                    text = "Plans are not configured yet."
                elif parsed is None:
                    text = "Use /tasks [page] or /tasks mine [page]."
                else:
                    mine, page = parsed
                    if mine and accepted.user_id is None:
                        text = "Please use your personal Telegram identity for /tasks mine."
                    else:
                        tasks, total = await self.plan_service.list_tasks(
                            accepted.chat_id,
                            page=page,
                            page_size=20,
                            assigned_user_id=accepted.user_id if mine else None,
                        )
                        text = render_task_page(tasks, total, page, mine)
            elif not invocation.request:
                text = "Use /ask followed by your question, or mention me with a request."
            elif accepted.user_id is None:
                text = "Please invoke me using your personal Telegram identity."
            else:
                if len(invocation.request) > self.settings.max_context_chars:
                    text = "That request is too long. Please send a shorter question."
                    status = "input_limited"
                else:
                    allowed = await self.repository.reserve_ai_request(
                        accepted.chat_id,
                        accepted.user_id,
                        self.settings.ai_user_requests_per_minute,
                        self.settings.ai_chat_requests_per_minute,
                    )
                    if not allowed:
                        text = (
                            "This group's or your AI request limit was reached. "
                            "Try again in a minute."
                        )
                        status = "rate_limited"
                    else:
                        try:
                            media = (
                                await self.media_service.load(message)
                                if self.media_service
                                else None
                            )
                        except MediaError as exc:
                            text, status, error_code = exc.user_message, "input_rejected", exc.code
                            media = None
                        else:
                            summary, summary_through = await self._refresh_summary(accepted)
                            try:
                                context = await self.context_service.build_context(
                                    accepted,
                                    message,
                                    invocation.request,
                                    summary,
                                    summary_through,
                                )
                                context.media_attachment = media
                            except ContextTooLarge:
                                text = "That request is too long. Please send a shorter question."
                                status = "input_limited"
                            else:
                                text, status, error_code = await self._run_ai(
                                    context, accepted, event
                                )
            try:
                for chunk in render_rich_chunks(text):
                    try:
                        outgoing = await self.telegram.send_message(
                            telegram_chat_id, chunk.html, accepted.telegram_message_id
                        )
                    except TelegramSendUncertain:
                        raise
                    except TelegramError as exc:
                        if exc.code != "formatting":
                            raise
                        outgoing = await self.telegram.send_message(
                            telegram_chat_id, chunk.plain, accepted.telegram_message_id, None
                        )
                    try:
                        await self.repository.save_outgoing(accepted.chat_id, outgoing)
                    except Exception:
                        # Telegram confirmed delivery, but its durable history is incomplete.
                        status, error_code = "uncertain", "outgoing_persistence_failed"
                        break
            except TelegramSendUncertain as exc:
                status, error_code = "uncertain", exc.code
            except TelegramError as exc:
                status, error_code = "delivery_failed", exc.code
            await self.repository.finish_update(update_id, status, error_code)
            logger.info(
                "update_finished", extra={**event, "status": status, "error_code": error_code}
            )
        except asyncio.CancelledError:
            # Cancellation during a send can mean delivery occurred. Never replay the claim.
            await self.repository.finish_update(update_id, "uncertain", "request_cancelled")
            raise
        except Exception:
            try:
                await self.repository.finish_update(update_id, "failed", "processing_error")
            except Exception:
                pass  # The durable claim still prevents duplicate external actions.
            raise

    async def _run_ai(
        self, context: AssistantContext, accepted: AcceptedMessage, event: dict[str, object]
    ) -> tuple[str, str, str | None]:
        started = perf_counter()
        response: AssistantResponse | None = None
        status, error_code = "completed", None
        try:
            response = await self.orchestrator.respond(context)
            text = render_assistant_response(response)
            if response.error_code:
                status, error_code = "ai_failed", response.error_code
        except GeminiError as exc:
            text = AI_FAILURE_TEXT
            status, error_code = "ai_failed", exc.code
        latency_ms = int((perf_counter() - started) * 1000)
        await self.repository.record_ai_run(
            chat_id=accepted.chat_id,
            trigger_message_id=accepted.message_id,
            model=self.settings.gemini_model,
            status="success" if response and not response.error_code else "failed",
            input_tokens=response.input_tokens if response else None,
            output_tokens=response.output_tokens if response else None,
            latency_ms=latency_ms,
            error_code=error_code,
            used_search=bool(response and response.used_search),
            used_maps=bool(response and response.used_maps),
            used_url_context=bool(response and response.used_url_context),
            used_code_execution=bool(response and response.used_code_execution),
            used_custom_functions=bool(response and response.used_custom_functions),
        )
        logger.info(
            "ai_finished",
            extra={
                **event,
                "model": self.settings.gemini_model,
                "latency_ms": latency_ms,
                "input_tokens": response.input_tokens if response else None,
                "output_tokens": response.output_tokens if response else None,
                "status": status,
            },
        )
        return text, status, error_code

    async def _refresh_summary(self, accepted: AcceptedMessage) -> tuple[str | None, int | None]:
        if self.summary_service is None or self.summary_gateway is None:
            return None, None
        batch = await self.summary_service.prepare(accepted.chat_id, accepted.telegram_message_id)
        if not batch.should_summarize:
            return (
                (batch.state.summary, batch.state.through_telegram_message_id)
                if batch.state
                else (None, None)
            )
        previous = batch.state.summary if batch.state else "(none)"
        prompt = (
            "Previous rolling summary:\n"
            + previous
            + "\n\nNew messages:\n"
            + "\n".join(message.prompt_text for message in batch.messages)
        )
        started = perf_counter()
        result: GeminiResult | None = None
        error_code = None
        summary: str | None
        checkpoint = batch.state.through_telegram_message_id if batch.state else None
        try:
            result = await self.summary_gateway.summarize(
                prompt=prompt,
                system_prompt=self.summary_system_prompt,
                max_output_tokens=self.settings.summary_max_output_tokens,
                timeout_seconds=self.settings.summary_timeout_seconds,
            )
            try:
                saved_state = await self.summary_service.save(
                    accepted.chat_id,
                    batch.state.version if batch.state else None,
                    result.text,
                    batch.messages[-1].id,
                )
                summary = saved_state.summary
                checkpoint = saved_state.through_telegram_message_id
            except SummaryConflict:
                current_state = await self.summary_service.current(accepted.chat_id)
                summary = current_state.summary if current_state else previous
                checkpoint = (
                    current_state.through_telegram_message_id if current_state else checkpoint
                )
        except GeminiError as exc:
            error_code = exc.code
            summary = previous if batch.state else None
        await self.repository.record_ai_run(
            chat_id=accepted.chat_id,
            trigger_message_id=accepted.message_id,
            model=self.settings.gemini_model,
            status="success" if result and error_code is None else "failed",
            input_tokens=result.input_tokens if result else None,
            output_tokens=result.output_tokens if result else None,
            latency_ms=int((perf_counter() - started) * 1000),
            error_code=error_code,
            request_type="summary",
        )
        return summary, checkpoint
