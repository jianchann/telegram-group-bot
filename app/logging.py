"""Minimal structured events; never serialize arbitrary exception/request objects."""

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

_invocation: ContextVar[dict[str, object] | None] = ContextVar("log_invocation", default=None)


@contextmanager
def invocation_logging(fields: dict[str, object]) -> Iterator[None]:
    token = _invocation.set(fields)
    try:
        yield
    finally:
        _invocation.reset(token)


def log_ai_event(logger: logging.Logger, event: str, **fields: object) -> None:
    logger.info(event, extra={**(_invocation.get() or {}), **fields})


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        event: dict[str, object] = {
            "time": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        for key in (
            "request_id",
            "chat_id",
            "update_id",
            "model",
            "latency_ms",
            "input_tokens",
            "output_tokens",
            "status",
            "error_code",
            "error_type",
            "stage",
            "turn_number",
            "attempt_number",
            "max_turns",
            "max_tool_calls",
            "tool_calls_executed",
            "allow_tools",
            "has_answer",
            "tool_names",
            "tool_name",
            "rejection_reason",
            "research_kind",
            "cache_reused",
            "research_steps",
            "research_eligible",
            "research_used",
            "limit_reason",
            "tools_disabled_reason",
        ):
            if hasattr(record, key):
                event[key] = getattr(record, key)
        return json.dumps(event)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    app_logger = logging.getLogger("app")
    app_logger.handlers = [handler]
    app_logger.setLevel(logging.INFO)
    app_logger.propagate = False
    # HTTPX's INFO logger includes the bot token in its outgoing request URL.
    for name in ("httpx", "httpcore", "google", "sqlalchemy.engine"):
        logging.getLogger(name).setLevel(logging.WARNING)
