import pytest

from app.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://test:test@localhost/test",
        telegram_bot_token="123:test_bot_token",
        telegram_webhook_secret="test_webhook_secret_123",
        allowed_telegram_chat_ids=[-1001],
        gcp_project_id="test-project",
    )
