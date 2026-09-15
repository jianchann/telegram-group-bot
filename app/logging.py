"""Minimal structured events; never serialize arbitrary exception/request objects."""

import json
import logging
from datetime import UTC, datetime


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
