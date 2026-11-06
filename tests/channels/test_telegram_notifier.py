"""TelegramNotifier keeps every message in the Notifications inbox too: sent, failed (once), or with no paired chat,
so the heartbeat's "Already told them lately" sees what schedules and notify_owner told the owner on Telegram."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from tests.capabilities.test_heartbeat import T0, on
from tests.daemon.test_telegram import BOT_TOKEN, OWNER, FakeTelegram
from tests.scripted import ScriptedModel, say
from wraithd.channels.telegram import OWNER_CHAT, TelegramBot, TelegramNotifier
from wraithd.daemon import heartbeat
from wraithd.tools.notify import INBOX


def inbox(store) -> list[str]:
    conversation = store.find_conversation(*INBOX)
    return [] if conversation is None else [m.content for m in store.messages(conversation.id)]


@pytest.fixture
async def notifier_for(isolated):
    bots = []

    def make(rt, handler, paired=True):
        bot = TelegramBot(BOT_TOKEN, transport=httpx.MockTransport(handler))
        bots.append(bot)
        if paired:
            rt.store.set_setting(OWNER_CHAT, str(OWNER))
        rt.notifier = TelegramNotifier(rt.store, bot)
        return rt.notifier

    yield make
    for bot in bots:
        await bot.close()


async def test_a_sent_message_is_also_kept_in_the_inbox(make_runtime, notifier_for):
    fake = FakeTelegram()
    notifier = notifier_for(make_runtime(ScriptedModel([])), fake.handler)
    assert await notifier.deliver('Your parcel arrived.') == 'telegram'
    assert [m['text'] for m in fake.sent(OWNER)] == ['Your parcel arrived.']
    assert inbox(notifier.store) == ['Your parcel arrived.']


async def test_a_failed_send_is_stored_once(make_runtime, notifier_for):
    def down(request):
        return httpx.Response(200, json={'ok': False, 'description': 'Bad Gateway'})

    notifier = notifier_for(make_runtime(ScriptedModel([])), down)
    assert await notifier.deliver('Rent is due Friday.') == 'inbox'
    assert inbox(notifier.store) == ['Rent is due Friday.']


async def test_without_a_paired_chat_it_is_stored_once(make_runtime, notifier_for):
    fake = FakeTelegram()
    notifier = notifier_for(make_runtime(ScriptedModel([])), fake.handler, paired=False)
    assert await notifier.deliver('A reminder.') == 'inbox'
    assert inbox(notifier.store) == ['A reminder.'] and fake.sent() == []


async def test_a_telegram_delivered_schedule_answer_is_in_the_next_heartbeats_already_told(make_runtime, home,
                                                                                           notifier_for):
    from wraithd.daemon.scheduler import Scheduler

    on(home)
    fake = FakeTelegram()
    model = ScriptedModel([[say('Your dentist is at 10:00.')], [say('Rent is due Friday.')], [say('HEARTBEAT_OK')]])
    rt = make_runtime(model)
    notifier_for(rt, fake.handler)
    beat = heartbeat.Heartbeat(rt)
    await beat.tick(T0)  # the heartbeat speaks: through Telegram, so into the inbox and its own run event
    now = datetime.now(UTC)
    rt.store.add_schedule('rent reminder', None, now.isoformat(), 'UTC', next_fire_at=now - timedelta(minutes=1))
    assert await Scheduler(rt).tick(now)
    sent = [m['text'] for m in fake.sent(OWNER)]
    assert sent[0] == 'Your dentist is at 10:00.' and 'Rent is due Friday.' in sent[1]
    assert beat.recently_told() == ['⏰ rent reminder Rent is due Friday.', 'Your dentist is at 10:00.']  # each once
    await beat.tick(T0 + timedelta(minutes=30))
    told = model.inputs[2][0]['content'].split('Already told them lately:\n')[1].split('\n\nChecklist')[0]
    assert told == '- ⏰ rent reminder Rent is due Friday.\n- Your dentist is at 10:00.'
