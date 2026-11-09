"""
Telegram quick actions with the fake Bot API: the [telegram.commands] menu, "/news" from the owner sent as its
message, and the fixed Post / Rewrite / Skip buttons under a draft card, which send text through the same path as
typed text (so create_post still asks).
"""

import asyncio
import logging
from datetime import timedelta

import httpx
import pytest

from tests.daemon.test_telegram import BOT_TOKEN, OWNER, STRANGER, FakeTelegram, paired
from tests.scripted import ScriptedModel, call, fake_tool, say
from wraithd import config
from wraithd.channels import base, telegram
from wraithd.core import events as ev
from wraithd.core import gate as gate_module

DRAFT = '✍️ D2 · reply\n\n> @someone: great slab\n\nThat PSA 10 is clean. Congrats!'


@pytest.fixture
def fake_telegram(isolated):
    config.set_key('telegram', BOT_TOKEN)
    return FakeTelegram()


@pytest.fixture(autouse=True)
def quick_polls(monkeypatch):
    monkeypatch.setattr(gate_module, 'APPROVAL_POLL_SECONDS', 0.01)


async def daemon_for(make_daemon, fake, model):
    daemon, _ = await make_daemon(model, telegram_transport=httpx.MockTransport(fake.handler))
    await paired(daemon, fake)
    return daemon


async def poll(daemon):
    await daemon.telegram.poll_once(timeout=0)
    await daemon.telegram.settle()


def ignored(daemon):
    return daemon.rt.store._query('SELECT payload FROM run_events WHERE type = ?', (ev.TELEGRAM_IGNORED,))


def answers(fake):
    return [p['text'] for m, p in fake.calls if m == 'answerCallbackQuery']


# --- the commands ------------------------------------------------------------------------------------------

def test_the_commands_table_defaults_to_five_and_skips_what_telegram_or_wraithd_wont_take(home, caplog):
    assert dict(config.load_config(home).telegram_commands) == {
        'news': 'run news-feed', 'drafts': 'slabbed drafts', 'jobs': 'find jobs',
        'watchlist': 'show my watch list', 'review': 'weekly review'}
    home.config_path.write_text(home.config_path.read_text() + '\n[telegram.commands]\nnews = "run news-feed"\n'
                                '"Bad-Name" = "x"\npair = "steal pairing"\nmcp_add = "x"\nempty = ""\n'
                                f'{"x" * 33} = "too long"\n')
    with caplog.at_level(logging.WARNING):
        assert config.load_config(home).telegram_commands == (('news', 'run news-feed'),)
    assert 'not a Telegram command name' in caplog.text and '/pair keeps its own meaning' in caplog.text


async def test_the_menu_is_registered_when_polling_starts(make_daemon, fake_telegram):
    daemon = await daemon_for(make_daemon, fake_telegram, ScriptedModel([]))

    async def waiting(timeout=30):  # the fake answers getUpdates at once: a real long poll waits
        await asyncio.sleep(1)

    daemon.telegram.poll_once = waiting
    polling = asyncio.create_task(daemon.telegram.run_forever())
    for _ in range(100):
        if any(m == 'setMyCommands' for m, _ in fake_telegram.calls):
            break
        await asyncio.sleep(0.01)
    polling.cancel()
    await asyncio.gather(polling, return_exceptions=True)
    [menu] = [p for m, p in fake_telegram.calls if m == 'setMyCommands']
    assert menu['commands'][0] == {'command': 'news', 'description': 'run news-feed'}
    assert [c['command'] for c in menu['commands']] == ['news', 'drafts', 'jobs', 'watchlist', 'review']


async def test_the_owners_news_runs_its_message_and_a_strangers_is_ignored(make_daemon, fake_telegram):
    model = ScriptedModel([[say('Here is the news.')], [say('With more.')]])
    daemon = await daemon_for(make_daemon, fake_telegram, model)
    fake_telegram.message(STRANGER, '/news')
    fake_telegram.message(OWNER, '/news')
    await poll(daemon)
    fake_telegram.message(OWNER, '/news@wraithd_bot tech only')
    await poll(daemon)
    assert [run.message for run in reversed(daemon.rt.store.recent_runs(5))] == ['run news-feed',
                                                                                  'run news-feed tech only']
    assert fake_telegram.sent(STRANGER) == []
    assert fake_telegram.sent(OWNER)[-2]['text'] == 'Here is the news.'
    assert ignored(daemon)


async def test_pair_cant_be_taken_and_a_forwarded_command_is_just_text(make_daemon, fake_telegram, home):
    home.config_path.write_text(home.config_path.read_text() + '\n[telegram.commands]\npair = "run news-feed"\n')
    model = ScriptedModel([[say('That is a forwarded line.')]])
    daemon = await daemon_for(make_daemon, fake_telegram, model)
    fake_telegram.message(OWNER, '/pair 123456')
    await poll(daemon)
    assert fake_telegram.sent(OWNER)[-1]['text'].startswith('There is no pairing code waiting')
    assert daemon.rt.store.recent_runs(5) == []
    await daemon.telegram.receive(base.Incoming(chat=OWNER, text='/news', forwarded=True))
    await daemon.telegram.settle()
    [run] = daemon.rt.store.recent_runs(5)
    assert run.message == '/news'


# --- draft buttons -----------------------------------------------------------------------------------------

def test_only_a_draft_card_gets_the_three_buttons():
    [[post, rewrite, skip]] = telegram.draft_buttons(DRAFT)
    assert (post, rewrite, skip) == ({'text': 'Post D2', 'callback_data': 'draft:2:post'},
                                     {'text': 'Rewrite', 'callback_data': 'draft:2:rewrite'},
                                     {'text': 'Skip', 'callback_data': 'draft:2:skip'})
    assert telegram.draft_buttons('✍ D17 · post\nbody')[0][0]['callback_data'] == 'draft:17:post'
    for text in ('Sent 1 item.', 'Here: ✍️ D2 · reply', '✍️ D123 · reply', '✍️ D2 reply', '✍️ Dx · reply'):
        assert telegram.draft_buttons(text) is None, text


async def test_a_draft_notification_gets_the_buttons(make_daemon, fake_telegram):
    daemon = await daemon_for(make_daemon, fake_telegram, ScriptedModel([]))
    await daemon.rt.notifier.deliver(DRAFT)
    await daemon.rt.notifier.deliver('Sent 1 item.')
    draft, plain = fake_telegram.sent(OWNER)[-2:]
    assert [b['text'] for b in draft['reply_markup']['inline_keyboard'][0]] == ['Post D2', 'Rewrite', 'Skip']
    assert 'reply_markup' not in plain


async def test_post_sends_post_d2_and_create_post_still_asks(make_daemon, fake_telegram, monkeypatch):
    posted = []
    model = ScriptedModel([[call('1', 'create_post', {'text': 'That PSA 10 is clean.'})], [say('Posted D2.')]])
    daemon = await daemon_for(make_daemon, fake_telegram, model)
    real_reply = base.reply
    monkeypatch.setattr(base, 'reply', lambda *a, **k: real_reply(
        *a, tools=[fake_tool('create_post', lambda **x: posted.append(x) or {'ok': True})], **k))
    fake_telegram.press(OWNER, 'draft:2:post')
    await daemon.telegram.poll_once(timeout=0)
    for _ in range(300):  # the run waits on create_post's card
        cards = [m for m in fake_telegram.sent(OWNER) if m['text'].startswith('Allow create_post?')]
        if cards:
            break
        await asyncio.sleep(0.01)
    assert cards and posted == []  # the button sent text; it approved nothing
    allow, _ = cards[0]['reply_markup']['inline_keyboard'][0]
    fake_telegram.press(OWNER, allow['callback_data'])
    await poll(daemon)
    assert posted == [{'text': 'That PSA 10 is clean.'}] and answers(fake_telegram) == ['Sent: post D2', 'Approved']
    assert daemon.rt.store.recent_runs(1)[0].message == 'post D2'


async def test_skip_sends_skip_d2(make_daemon, fake_telegram):
    daemon = await daemon_for(make_daemon, fake_telegram, ScriptedModel([[say('Skipped D2.')]]))
    fake_telegram.press(OWNER, 'draft:2:skip')
    await poll(daemon)
    assert daemon.rt.store.recent_runs(1)[0].message == 'skip D2'
    assert fake_telegram.sent(OWNER)[-1]['text'] == 'Skipped D2.'


async def test_rewrite_asks_how_and_the_reply_is_the_how(make_daemon, fake_telegram):
    daemon = await daemon_for(make_daemon, fake_telegram, ScriptedModel([[say('New D2: ...')], [say('Hi.')]]))
    fake_telegram.press(OWNER, 'draft:2:rewrite')
    await poll(daemon)
    assert fake_telegram.sent(OWNER)[-1]['text'] == 'How should I rewrite D2? Reply with what to change.'
    assert daemon.rt.store.recent_runs(1) == []
    fake_telegram.message(OWNER, 'shorter, no emoji')
    await poll(daemon)
    fake_telegram.message(OWNER, 'hello')  # only the next message
    await poll(daemon)
    assert [run.message for run in reversed(daemon.rt.store.recent_runs(5))] == [
        'rewrite D2 shorter, no emoji', 'hello']


async def test_a_command_or_time_cancels_a_rewrite(make_daemon, fake_telegram):
    model = ScriptedModel([[say('News.')], [say('Hi.')]])
    daemon = await daemon_for(make_daemon, fake_telegram, model)
    fake_telegram.press(OWNER, 'draft:3:rewrite')
    await poll(daemon)
    fake_telegram.message(OWNER, '/news')
    await poll(daemon)
    fake_telegram.press(OWNER, 'draft:3:rewrite')
    await poll(daemon)
    daemon.telegram.rewriting = (3, daemon.telegram.clock() - timedelta(seconds=1))  # over 5 minutes ago
    fake_telegram.message(OWNER, 'hello')
    await poll(daemon)
    assert [run.message for run in reversed(daemon.rt.store.recent_runs(5))] == ['run news-feed', 'hello']


async def test_a_forged_tap_does_nothing(make_daemon, fake_telegram):
    daemon = await daemon_for(make_daemon, fake_telegram, ScriptedModel([]))
    for chat, data in ((STRANGER, 'draft:2:post'), (OWNER, 'draft:123:post'), (OWNER, 'draft:2:delete'),
                       (OWNER, 'draft:2:post:x'), (OWNER, 'draft:-1:skip'), (OWNER, 'draft:2: post')):
        fake_telegram.press(chat, data)
    await poll(daemon)
    assert daemon.rt.store.recent_runs(5) == [] and daemon.telegram.rewriting is None
    assert fake_telegram.sent(STRANGER) == [] and set(answers(fake_telegram)) == {'Only the owner can decide.'}
    assert ignored(daemon)
