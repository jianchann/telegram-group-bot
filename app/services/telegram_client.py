"""Direct async Telegram client with credential-free public errors."""

from typing import Any
from urllib.parse import quote

import httpx


class TelegramError(Exception):
    def __init__(self, code: str, retry_after: int | None = None) -> None:
        self.code = code
        self.retry_after = retry_after
        super().__init__(f"Telegram request failed ({code})")


class TelegramSendUncertain(TelegramError):
    """Delivery may have succeeded; callers must not resend automatically."""


class TelegramClient:
    def __init__(
        self,
        token: str,
        timeout_seconds: float = 10,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._file_base_url = f"https://api.telegram.org/file/bot{token}"
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient()
        self._timeout = timeout_seconds

    async def _call(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = await self._client.post(
                f"{self._base_url}/{method}", json=payload or {}, timeout=self._timeout
            )
        except httpx.TransportError:
            error = (
                TelegramSendUncertain if method in {"sendMessage", "sendPoll"} else TelegramError
            )
            raise error("transport") from None
        try:
            data = response.json()
        except ValueError:
            error = (
                TelegramSendUncertain if method in {"sendMessage", "sendPoll"} else TelegramError
            )
            raise error("invalid_response") from None
        if not isinstance(data, dict) or type(data.get("ok")) is not bool:
            error = (
                TelegramSendUncertain if method in {"sendMessage", "sendPoll"} else TelegramError
            )
            raise error("invalid_response")
        if response.is_error or not data.get("ok"):
            description = str(data.get("description", "")).lower()
            error_code = data.get("error_code")
            code = (
                "formatting"
                if response.status_code == 400
                and ("parse entities" in description or "unsupported start tag" in description)
                else str(error_code if isinstance(error_code, int) else response.status_code)
            )
            parameters = data.get("parameters")
            retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
            if not isinstance(retry_after, int):
                retry_after = None
            raise TelegramError(code, retry_after=retry_after)
        result = data.get("result")
        return result if isinstance(result, dict) else {"result": result}

    async def get_me(self) -> dict[str, Any]:
        return await self._call("getMe")

    async def get_file(self, file_id: str) -> dict[str, Any]:
        result = await self._call("getFile", {"file_id": file_id})
        if not isinstance(result.get("file_path"), str) or not result["file_path"]:
            raise TelegramError("invalid_response")
        return result

    async def download_file(self, file_path: str, max_bytes: int) -> bytes:
        try:
            url = f"{self._file_base_url}/{quote(file_path, safe='/')}"
            async with self._client.stream("GET", url, timeout=self._timeout) as response:
                if response.is_error:
                    raise TelegramError(str(response.status_code))
                length = response.headers.get("content-length")
                if length and length.isdigit() and int(length) > max_bytes:
                    raise TelegramError("file_too_large")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise TelegramError("file_too_large")
                return bytes(body)
        except TelegramError:
            raise
        except httpx.TransportError:
            raise TelegramError("transport") from None

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        parse_mode: str | None = "HTML",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_to_message_id is not None:
            payload["reply_parameters"] = {"message_id": reply_to_message_id}
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        result = await self._call("sendMessage", payload)
        chat = result.get("chat")
        if (
            type(result.get("message_id")) is not int
            or type(result.get("date")) is not int
            or not isinstance(chat, dict)
            or type(chat.get("id")) is not int
            or chat["id"] != chat_id
        ):
            raise TelegramSendUncertain("invalid_response")
        return result

    async def send_poll(
        self,
        chat_id: int,
        question: str,
        options: list[str],
        is_anonymous: bool = False,
        allows_multiple_answers: bool = False,
    ) -> dict[str, Any]:
        result = await self._call(
            "sendPoll",
            {
                "chat_id": chat_id,
                "question": question,
                "options": [{"text": option} for option in options],
                "is_anonymous": is_anonymous,
                "allows_multiple_answers": allows_multiple_answers,
            },
        )
        chat = result.get("chat")
        poll = result.get("poll")
        if (
            type(result.get("message_id")) is not int
            or type(result.get("date")) is not int
            or not isinstance(chat, dict)
            or type(chat.get("id")) is not int
            or chat["id"] != chat_id
            or not isinstance(poll, dict)
            or not isinstance(poll.get("id"), str)
        ):
            raise TelegramSendUncertain("invalid_response")
        return result

    async def set_webhook(self, url: str, secret: str) -> dict[str, Any]:
        return await self._call("setWebhook", {"url": url, "secret_token": secret})

    async def get_webhook_info(self) -> dict[str, Any]:
        return await self._call("getWebhookInfo")

    async def get_updates(self) -> list[dict[str, Any]]:
        """Inspect pending updates without advancing their acknowledgment offset."""
        result = (await self._call("getUpdates", {"timeout": 0})).get("result")
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise TelegramError("invalid_response")
        return result

    async def delete_webhook(self) -> dict[str, Any]:
        return await self._call("deleteWebhook")

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
