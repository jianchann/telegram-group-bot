import pytest
from pydantic import ValidationError

from app.config import Settings


def test_process_environment_and_defaults(monkeypatch):
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:password@localhost/test")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:testtoken")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "a_secret_of_16_chars")
    monkeypatch.setenv("ALLOWED_TELEGRAM_CHAT_IDS", "[-1001, -1002]")
    monkeypatch.setenv("GCP_PROJECT_ID", "test-project")
    config = Settings()
    assert config.allowed_telegram_chat_ids == frozenset({-1001, -1002})
    assert config.gemini_model == "gemini-3.5-flash-lite"
    assert config.default_chat_timezone == "Asia/Manila"
    assert config.max_recent_messages == 30
    assert config.max_url_context_urls == 5
    assert config.max_ai_output_tokens == 1024
    assert config.max_ai_turns == 4
    assert config.max_ai_tool_calls == 8
    assert "password" not in repr(config)
    assert "testtoken" not in repr(config)


@pytest.mark.parametrize(
    "change",
    [
        {"allowed_telegram_chat_ids": []},
        {"allowed_telegram_chat_ids": [123]},
        {"allowed_telegram_chat_ids": [True]},
        {"telegram_webhook_secret": "short"},
        {"telegram_webhook_secret": "not allowed spaces"},
        {"telegram_bot_token": "not-a-bot-token"},
        {"database_url": "sqlite:///example.db"},
        {"database_url": "postgresql://"},
        {"gcp_project_id": "   "},
        {"default_chat_timezone": "Invalid/Timezone"},
        {"ai_user_requests_per_minute": 0},
        {"max_url_context_urls": 21},
        {"webhook_timeout_seconds": 10},
    ],
)
def test_invalid_configuration_fails_without_disclosing_inputs(settings, change):
    arguments = settings.model_dump()
    arguments.update(change)
    with pytest.raises(ValidationError) as error:
        Settings(**arguments)
    assert "input_value" not in str(error.value)
    assert "test_bot_token" not in str(error.value)


def test_settings_does_not_load_env_files():
    assert Settings.model_config["env_file"] is None
