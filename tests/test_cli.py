import argparse
import io
import json
from unittest.mock import AsyncMock

import pytest

from app import cli


def update(chat_id=-10042, chat_type="supergroup"):
    return {
        "update_id": 99,
        "message": {"chat": {"id": chat_id, "type": chat_type}, "text": "private content"},
    }


@pytest.mark.parametrize("bad", [True, "99", None])
def test_group_id_rejects_invalid_update_id(bad):
    sample = update()
    sample["update_id"] = bad
    with pytest.raises(ValueError):
        cli.group_id(sample)


def test_group_id_validates_id_and_ignores_non_groups():
    assert cli.group_id(update()) == -10042
    assert cli.group_id(update(chat_type="private")) is None
    with pytest.raises(ValueError):
        cli.group_id(update(chat_id=True))


async def test_identify_chat_needs_no_settings(monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(update())))
    await cli.run_command(argparse.Namespace(command="identify-chat"))
    assert capsys.readouterr().out == "-10042\n"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-secret-token")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "fake-secret-header")
    fake = AsyncMock()
    monkeypatch.setattr(cli, "TelegramClient", lambda *args, **kwargs: fake)
    return fake


async def test_webhook_info_is_sanitized(client, capsys):
    client.get_webhook_info.return_value = {
        "url": "https://example.test/private-secret",
        "last_error_message": "fake-secret-token",
        "pending_update_count": 7,
    }
    await cli.run_command(argparse.Namespace(command="webhook-info"))
    output = capsys.readouterr().out
    assert "secret" not in output
    assert json.loads(output) == {
        "configured": True,
        "pending_update_count": 7,
        "last_error_date": None,
    }
    client.close.assert_awaited_once()


async def test_discover_refuses_active_webhook(client):
    client.get_webhook_info.return_value = {"url": "https://example.test/hook"}
    with pytest.raises(ValueError):
        await cli.run_command(argparse.Namespace(command="discover-chats"))
    client.get_updates.assert_not_awaited()
    client.close.assert_awaited_once()


async def test_discover_prints_only_unique_group_ids(client, capsys):
    client.get_webhook_info.return_value = {"url": ""}
    client.get_updates.return_value = [update(), update(), update(chat_type="private")]
    await cli.run_command(argparse.Namespace(command="discover-chats"))
    assert capsys.readouterr().out == "-10042\n"
    client.delete_webhook.assert_not_awaited()
    client.close.assert_awaited_once()


@pytest.mark.parametrize(
    "url",
    ["http://example.test", "https://user:pass@example.test", "https://example.test?token=secret"],
)
async def test_webhook_set_rejects_unsafe_urls(client, url):
    with pytest.raises(ValueError):
        await cli.run_command(argparse.Namespace(command="webhook-set", url=url))
    client.set_webhook.assert_not_awaited()


async def test_webhook_set_passes_explicit_secret(client):
    await cli.run_command(
        argparse.Namespace(command="webhook-set", url="https://example.test/hook")
    )
    client.set_webhook.assert_awaited_once_with("https://example.test/hook", "fake-secret-header")
