from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.schemas.summary import SummaryConflict, SummaryMessage
from app.services.summary_service import SummaryService


def state(summary="Earlier context", version=1):
    return SimpleNamespace(summary=summary, version=version)


@pytest.mark.asyncio
async def test_thresholds_are_strict_and_do_not_fetch_batch_until_crossed():
    repository = SimpleNamespace(
        get=AsyncMock(return_value=None),
        unsummarized_stats=AsyncMock(side_effect=[(100, 40_000), (101, 1)]),
        batch=AsyncMock(return_value=[SummaryMessage(uuid4(), 1, "Ada", "Hello", False)]),
    )
    service = SummaryService(repository)
    first = await service.prepare(uuid4(), 200)
    second = await service.prepare(uuid4(), 200)
    assert not first.should_summarize
    assert second.should_summarize
    repository.batch.assert_awaited_once()


@pytest.mark.asyncio
async def test_current_returns_saved_state():
    existing = state()
    repository = SimpleNamespace(get=AsyncMock(return_value=existing))
    assert await SummaryService(repository).current(uuid4()) is existing


@pytest.mark.asyncio
async def test_previous_summary_consumes_input_budget_and_empty_batch_defers():
    existing = state("x" * 100)
    repository = SimpleNamespace(
        get=AsyncMock(return_value=existing),
        unsummarized_stats=AsyncMock(return_value=(101, 500)),
        batch=AsyncMock(return_value=[]),
    )
    result = await SummaryService(repository, max_input_chars=250).prepare(uuid4(), 300)
    assert not result.should_summarize
    assert result.state is existing
    assert repository.batch.await_args.args[-1] == 149


@pytest.mark.asyncio
async def test_save_strips_text_and_reports_compare_and_swap_conflict():
    saved = SimpleNamespace(summary="Updated")
    repository = SimpleNamespace(compare_and_swap=AsyncMock(return_value=saved))
    service = SummaryService(repository)
    chat_id, message_id = uuid4(), uuid4()
    assert await service.save(chat_id, 2, "  Updated  ", message_id) is saved
    repository.compare_and_swap.assert_awaited_once_with(chat_id, 2, "Updated", message_id)
    repository.compare_and_swap.return_value = None
    with pytest.raises(SummaryConflict):
        await service.save(chat_id, 2, "Another", message_id)


@pytest.mark.asyncio
async def test_empty_summary_is_rejected_before_storage():
    repository = SimpleNamespace(compare_and_swap=AsyncMock())
    with pytest.raises(ValueError):
        await SummaryService(repository).save(uuid4(), None, "  ", uuid4())
    repository.compare_and_swap.assert_not_awaited()
