"""The scheduler's delivery: a NO_REPLY answer is silent, anything else reaches the owner with its ⏰ header."""

from datetime import UTC, datetime, timedelta

import pytest

from tests.scripted import ScriptedModel, say
from wraithd.daemon.scheduler import SILENT, Scheduler


class FakeNotifier:
    channel = 'fake'

    def __init__(self):
        self.sent = []

    async def deliver(self, text: str) -> str:
        self.sent.append(text)
        return self.channel


def due_schedule(rt, message: str):
    now = datetime.now(UTC)
    rt.store.add_schedule(message, None, now.isoformat(), 'UTC', next_fire_at=now - timedelta(minutes=1))
    return now


@pytest.mark.parametrize('answer', [SILENT, f'  {SILENT}\n'])
async def test_a_no_reply_answer_sends_nothing(make_runtime, answer):
    rt = make_runtime(ScriptedModel([[say(answer)]]))
    rt.notifier = FakeNotifier()
    now = due_schedule(rt, 'run news-feed (scheduled)')
    [run_id] = await Scheduler(rt).tick(now)
    assert rt.notifier.sent == []
    assert run_id  # the run itself is still logged


@pytest.mark.parametrize('answer', ['Rent is due Friday.', f'{SILENT}: nothing new', f'Sent 3 items. {SILENT}'])
async def test_any_other_answer_still_sends(make_runtime, answer):
    rt = make_runtime(ScriptedModel([[say(answer)]]))
    rt.notifier = FakeNotifier()
    now = due_schedule(rt, 'rent reminder')
    assert await Scheduler(rt).tick(now)
    assert rt.notifier.sent == [f'⏰ rent reminder\n\n{answer}']
