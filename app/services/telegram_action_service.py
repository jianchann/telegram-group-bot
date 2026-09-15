"""Chat-bound Telegram actions that persist confirmed outgoing messages."""

from typing import Any, Protocol
from uuid import UUID


class TelegramActionError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class TelegramActionRepository(Protocol):
    async def telegram_chat_id(
        self, chat_id: UUID, actor_id: UUID, source_message_id: UUID
    ) -> int | None: ...

    async def save_outgoing(self, chat_id: UUID, message: dict[str, Any]) -> None: ...


class PollSender(Protocol):
    async def send_poll(
        self,
        chat_id: int,
        question: str,
        options: list[str],
        is_anonymous: bool = False,
        allows_multiple_answers: bool = False,
    ) -> dict[str, Any]: ...


class TelegramActionService:
    def __init__(self, repository: TelegramActionRepository, telegram: PollSender) -> None:
        self.repository = repository
        self.telegram = telegram

    async def create_poll(
        self,
        chat_id: UUID,
        actor_id: UUID,
        source_message_id: UUID,
        question: str,
        options: list[str],
        is_anonymous: bool = False,
        allows_multiple_answers: bool = False,
    ) -> dict[str, Any]:
        telegram_chat_id = await self.repository.telegram_chat_id(
            chat_id, actor_id, source_message_id
        )
        if telegram_chat_id is None:
            raise TelegramActionError("chat_not_found")
        outgoing = await self.telegram.send_poll(
            telegram_chat_id,
            question,
            options,
            is_anonymous,
            allows_multiple_answers,
        )
        try:
            await self.repository.save_outgoing(chat_id, outgoing)
        except Exception:
            # Telegram confirmed delivery. Do not risk creating a duplicate poll.
            raise TelegramActionError("outgoing_persistence_failed") from None
        return outgoing
