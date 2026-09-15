"""Native Telegram tools guarded by direct current-user intent."""

import json
import re
import unicodedata
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.schemas.ai import AssistantContext, ToolCall, ToolDeclaration, ToolResult
from app.services.telegram_action_service import TelegramActionError, TelegramActionService
from app.services.telegram_client import TelegramError, TelegramSendUncertain

PollOption = Annotated[str, Field(min_length=1, max_length=100)]


class PollArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    question: str = Field(min_length=1, max_length=300)
    options: list[PollOption] = Field(min_length=2, max_length=10)
    is_anonymous: bool = False
    allows_multiple_answers: bool = False

    @field_validator("question")
    @classmethod
    def clean_question(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("blank question")
        return value.strip()

    @field_validator("options")
    @classmethod
    def clean_options(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("blank option")
        normalized = [unicodedata.normalize("NFKC", value).casefold() for value in cleaned]
        if len(set(normalized)) != len(normalized):
            raise ValueError("duplicate option")
        return cleaned


def _direct_request(text: str) -> str:
    text = re.sub(r"```.*?```|`[^`]*`|\"[^\"]*\"", " ", text, flags=re.S)
    text = re.sub(r"(?<!\w)'[^'\n]+'(?!\w)", " ", text)
    text = " ".join(line for line in text.splitlines() if not line.lstrip().startswith(">"))
    text = re.sub(r"@\w+", " ", text)
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def has_poll_intent(text: str) -> bool:
    request = _direct_request(text)
    if re.search(r"\b(?:do not|don't|never|without)\b.{0,50}\b(?:poll|vote|survey)\b", request):
        return False
    if re.search(r"\b(?:example|quoted?|code|instructions?)\b", request):
        return False
    return bool(
        re.search(
            r"\b(?:create|make|start|send|run|put up|set up|let'?s|lets)\b"
            r".{0,60}\b(?:poll|vote|survey)\b|\bvote (?:on|between|for)\b",
            request,
        )
    )


class TelegramToolExecutor:
    def __init__(self, context: AssistantContext, service: TelegramActionService) -> None:
        self.context = context
        self.service = service
        self.actions: list[dict[str, Any]] = []
        self.attempted: set[tuple[str, tuple[str, ...], bool, bool]] = set()
        self.declarations = [
            ToolDeclaration(
                "create_poll",
                "Create a native regular Telegram poll in the current group only when the "
                "current user directly asks to poll or vote. Use 2-10 concise unique options. "
                "Do not use this for examples, quoted requests, or passive suggestions.",
                PollArguments.model_json_schema(),
            )
        ]

    async def execute(self, call: ToolCall) -> ToolResult:
        try:
            if call.name != "create_poll":
                return ToolResult(call.name, {"ok": False, "error": "unknown_tool"}, call.call_id)
            args = PollArguments.model_validate_json(json.dumps(call.arguments))
            if self.context.current_user_id is None or self.context.current_message_id is None:
                raise TelegramActionError("identity_required")
            if not has_poll_intent(self.context.current_message):
                raise TelegramActionError("explicit_poll_intent_required")
            signature = (
                args.question,
                tuple(args.options),
                args.is_anonymous,
                args.allows_multiple_answers,
            )
            if signature in self.attempted:
                raise TelegramActionError("poll_already_attempted")
            self.attempted.add(signature)
            outgoing = await self.service.create_poll(
                self.context.chat_id,
                self.context.current_user_id,
                self.context.current_message_id,
                args.question,
                args.options,
                args.is_anonymous,
                args.allows_multiple_answers,
            )
            poll = outgoing["poll"]
            action = {
                "kind": "telegram_poll",
                "action": "created",
                "entity": {"question": args.question},
            }
            self.actions.append(action)
            result = {
                "ok": True,
                **action,
                "telegram_message_id": outgoing["message_id"],
                "poll_id": poll["id"],
            }
        except ValidationError:
            result = {"ok": False, "error": "invalid_tool_arguments"}
        except TelegramSendUncertain as error:
            result = {"ok": False, "error": error.code, "delivery_uncertain": True}
        except TelegramError as error:
            result = {"ok": False, "error": error.code}
        except TelegramActionError as error:
            result = {"ok": False, "error": error.code}
        return ToolResult(call.name, result, call.call_id)
