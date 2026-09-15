"""Telegram invocation detection using entity offsets measured in UTF-16."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Invocation:
    kind: str
    request: str


def _entity_text(text: str, entity: dict[str, Any]) -> str:
    try:
        start = int(entity["offset"]) * 2
        end = start + int(entity["length"]) * 2
        return text.encode("utf-16-le")[start:end].decode("utf-16-le")
    except (KeyError, ValueError, TypeError, UnicodeError):
        return ""


def detect_invocation(message: dict[str, Any], bot_id: int, bot_username: str) -> Invocation | None:
    if (message.get("from") or {}).get("is_bot"):
        return None
    text = message.get("text") or message.get("caption") or ""
    if not isinstance(text, str) or not text.strip():
        return None
    entities = message.get("entities") or message.get("caption_entities") or []
    # A leading command exclusively determines routing, even if it also mentions us.
    if text.startswith("/"):
        parts = text.split(maxsplit=1)
        command = parts[0]
        request = parts[1] if len(parts) > 1 else ""
        command_name, _, addressed_to = command.partition("@")
        if addressed_to and addressed_to.casefold() != bot_username.lstrip("@").casefold():
            return None
        kind = {
            "/ask": "ai",
            "/help": "help",
            "/start": "start",
            "/memory": "memory",
            "/memory_me": "memory_me",
            "/plans": "plans",
            "/plan": "plan",
            "/tasks": "tasks",
        }.get(command_name)
        return Invocation(kind, request.strip()) if kind else None
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        if entity.get("type") == "mention":
            if _entity_text(text, entity).casefold() == "@" + bot_username.lstrip("@").casefold():
                return Invocation("ai", text.strip())
        elif (
            entity.get("type") == "text_mention"
            and isinstance(entity.get("user"), dict)
            and entity["user"].get("id") == bot_id
        ):
            return Invocation("ai", text.strip())
    if ((message.get("reply_to_message") or {}).get("from") or {}).get("id") == bot_id:
        return Invocation("ai", text.strip())
    return None
