"""FastAPI entry point. Importing this module does not read credentials."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from hmac import compare_digest
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy import text

from app.ai.tools.composite import CompositeToolExecutor
from app.ai.tools.memory import MemoryToolExecutor
from app.ai.tools.plan import PlanToolExecutor
from app.ai.tools.telegram import TelegramToolExecutor
from app.config import Settings
from app.db.session import Database
from app.logging import configure_logging
from app.repositories.bot import BotRepository
from app.repositories.summaries import SummaryRepository
from app.schemas.telegram import TelegramUpdate
from app.services.ai_orchestrator import AIOrchestrator
from app.services.context_service import ContextService
from app.services.gemini_gateway import GoogleGeminiGateway
from app.services.media_service import MediaService
from app.services.memory_service import MemoryService
from app.services.plan_service import PlanService
from app.services.research_router import ResearchRouter
from app.services.summary_service import SummaryService
from app.services.telegram_action_service import TelegramActionService
from app.services.telegram_client import TelegramClient
from app.services.telegram_service import TelegramService

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, service: TelegramService | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        configure_logging()
        # BaseSettings supplies required constructor fields from process environment.
        config = settings or Settings()  # type: ignore[call-arg]
        application.state.settings = config
        if service is not None:
            application.state.service = service
            yield
            return
        database = Database(config.database_url.get_secret_value())
        telegram = TelegramClient(
            config.telegram_bot_token.get_secret_value(), config.telegram_timeout_seconds
        )
        gateway: GoogleGeminiGateway | None = None
        try:
            async with database.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            identity = await telegram.get_me()
            if type(identity.get("id")) is not int or not identity.get("username"):
                raise ValueError("Invalid Telegram bot identity")
            gateway = GoogleGeminiGateway(config)
            repository = BotRepository(database)
            memory_service = MemoryService(database)
            plan_service = PlanService(database)
            summary_service = SummaryService(
                SummaryRepository(database),
                message_threshold=config.summary_message_threshold,
                char_threshold=config.summary_char_threshold,
                max_input_chars=config.summary_max_input_chars,
            )
            telegram_action_service = TelegramActionService(repository, telegram)
            system_prompt = (Path(__file__).parent / "prompts" / "assistant_system.md").read_text()
            application.state.service = TelegramService(
                settings=config,
                repository=repository,
                context_service=ContextService(
                    repository,
                    config.max_recent_messages,
                    config.max_context_chars,
                    memory_service,
                    config.max_memory_results,
                    config.auto_memory_enabled,
                    plan_service,
                ),
                orchestrator=AIOrchestrator(
                    gateway,
                    system_prompt,
                    executor_factory=lambda context: CompositeToolExecutor(
                        MemoryToolExecutor(context, memory_service),
                        PlanToolExecutor(context, plan_service),
                        TelegramToolExecutor(context, telegram_action_service),
                    ),
                    timeout_seconds=config.gemini_timeout_seconds,
                    max_output_tokens=config.max_ai_output_tokens,
                    research_router=ResearchRouter(config.max_url_context_urls),
                    max_turns=config.max_ai_turns,
                    max_tool_calls=config.max_ai_tool_calls,
                ),
                telegram=telegram,
                bot_id=identity["id"],
                bot_username=identity["username"],
                memory_service=memory_service,
                plan_service=plan_service,
                media_service=MediaService(telegram, config.max_media_bytes),
                summary_service=summary_service,
                summary_gateway=gateway,
                summary_system_prompt=(
                    Path(__file__).parent / "prompts" / "summary_system.md"
                ).read_text(),
            )
            logger.info("startup_ready")
            yield
        except Exception as exc:
            logger.error("application_failure", extra={"error_type": type(exc).__name__})
            raise RuntimeError(
                "Bot startup or shutdown failed; check database, bot token, and ADC"
            ) from None
        finally:
            for resource in (gateway, telegram, database):
                if resource is not None:
                    try:
                        await resource.close()
                    except Exception as exc:
                        logger.error("cleanup_failed", extra={"error_type": type(exc).__name__})

    application = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    # HTTP test transports can inject dependencies without external startup calls.
    if settings is not None:
        application.state.settings = settings
    if service is not None:
        application.state.service = service

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.post("/telegram/webhook")
    async def webhook(request: Request) -> dict[str, bool]:
        config: Settings = request.app.state.settings
        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not compare_digest(
            supplied.encode(), config.telegram_webhook_secret.get_secret_value().encode()
        ):
            raise HTTPException(status_code=403, detail="Invalid webhook secret")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 1_000_000:
                raise HTTPException(status_code=413, detail="Update too large")
        try:
            update = TelegramUpdate.model_validate_json(bytes(body))
        except ValidationError:
            raise HTTPException(status_code=400, detail="Invalid Telegram update") from None
        payload: dict[str, Any] = update.model_dump(by_alias=True, exclude_none=True)
        try:
            async with asyncio.timeout(config.webhook_timeout_seconds):
                await request.app.state.service.process(payload)
        except TimeoutError:
            logger.warning("webhook_timeout", extra={"update_id": update.update_id})
            raise HTTPException(status_code=503, detail="Processing timed out") from None
        except Exception as exc:
            logger.error(
                "webhook_failed",
                extra={"update_id": update.update_id, "error_type": type(exc).__name__},
            )
            raise HTTPException(status_code=503, detail="Processing unavailable") from None
        return {"ok": True}

    return application


app = create_app()
