"""Explicit Telegram setup commands; never load project credential files."""

import argparse
import asyncio
import json
import sys
from typing import Any
from urllib.parse import urlsplit

from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.services.telegram_client import TelegramClient


class CLISettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", hide_input_in_errors=True)

    telegram_bot_token: SecretStr
    telegram_webhook_secret: SecretStr | None = None
    telegram_timeout_seconds: float = 10.0


def group_id(update: Any) -> int | None:
    """Extract only a validated group ID; do not expose message content."""
    if not isinstance(update, dict) or type(update.get("update_id")) is not int:
        raise ValueError("Expected a Telegram update object with integer update_id")
    message = update.get("message", update.get("edited_message"))
    if not isinstance(message, dict):
        return None
    chat = message.get("chat")
    if not isinstance(chat, dict) or chat.get("type") not in {"group", "supergroup"}:
        return None
    if type(chat.get("id")) is not int:
        raise ValueError("Expected integer group chat ID")
    return int(chat["id"])


async def run_command(args: argparse.Namespace) -> None:
    if args.command == "identify-chat":
        chat_id = group_id(json.load(sys.stdin))
        if chat_id is None:
            raise ValueError("Update contains no group message")
        print(chat_id)
        return
    settings = CLISettings()  # type: ignore[call-arg]  # BaseSettings reads process env.
    token = settings.telegram_bot_token.get_secret_value()
    if not token.strip():
        raise ValueError("TELEGRAM_BOT_TOKEN is required")
    client = TelegramClient(token, timeout_seconds=settings.telegram_timeout_seconds)
    try:
        if args.command == "webhook-set":
            parsed = urlsplit(args.url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("Webhook URL must be HTTPS without credentials or query")
            secret = settings.telegram_webhook_secret
            if secret is None or not secret.get_secret_value():
                raise ValueError("TELEGRAM_WEBHOOK_SECRET is required")
            await client.set_webhook(args.url, secret.get_secret_value())
            print("Webhook registered; pending updates were preserved.")
        elif args.command == "webhook-info":
            info = await client.get_webhook_info()
            # Provider error descriptions and URLs can contain sensitive values.
            print(
                json.dumps(
                    {
                        "configured": bool(info.get("url")),
                        "pending_update_count": info.get("pending_update_count", 0),
                        "last_error_date": info.get("last_error_date"),
                    }
                )
            )
        elif args.command == "webhook-delete":
            await client.delete_webhook()
            print("Webhook removed; pending updates were preserved.")
        elif args.command == "discover-chats":
            if (await client.get_webhook_info()).get("url"):
                raise ValueError("Remove the webhook before discovering pending chats")
            ids = {
                chat_id
                for update in await client.get_updates()
                if (chat_id := group_id(update)) is not None
            }
            for chat_id in sorted(ids):
                print(chat_id)
            if not ids:
                print(
                    "No pending group updates. Send a group message and retry.",
                    file=sys.stderr,
                )
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("identify-chat", help="Read one update JSON from stdin and print group ID")
    commands.add_parser(
        "discover-chats", help="Print pending group IDs without acknowledging updates"
    )
    webhook = commands.add_parser("webhook-set", help="Register an HTTPS webhook")
    webhook.add_argument("--url", required=True)
    commands.add_parser("webhook-info", help="Print sanitized webhook status")
    commands.add_parser("webhook-delete", help="Remove webhook without dropping updates")
    args = parser.parse_args()
    try:
        asyncio.run(run_command(args))
    except (ValueError, ValidationError):
        print(
            "Invalid configuration, URL, or update. Check required environment variables.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except Exception:
        print(
            "Telegram setup failed. Check network, credentials, and webhook state.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
