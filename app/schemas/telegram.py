"""Flexible inbound Bot API boundary models."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TelegramChat(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True, hide_input_in_errors=True)

    id: int
    type: str


class TelegramMessage(BaseModel):
    model_config = ConfigDict(
        extra="allow", populate_by_name=True, strict=True, hide_input_in_errors=True
    )

    message_id: int
    date: int
    chat: TelegramChat
    sender: dict[str, Any] | None = Field(default=None, alias="from")
    text: str | None = None
    caption: str | None = None
    entities: list[dict[str, Any]] = Field(default_factory=list)
    caption_entities: list[dict[str, Any]] = Field(default_factory=list)
    reply_to_message: dict[str, Any] | None = None


class TelegramUpdate(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True, hide_input_in_errors=True)

    update_id: int
    message: TelegramMessage | None = None
    edited_message: TelegramMessage | None = None
