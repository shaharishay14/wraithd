"""
NO_REPLY delivers nothing, on every channel (core/silent.py): the streamed text that could still become NO_REPLY
waits and is dropped when it does, the terminal prints nothing, and the conversation keeps no assistant message.
"""

from tests.scripted import ScriptedModel, ScriptedTerminal, call, say
from wraithd.channels import terminal
from wraithd.core.silent import NO_REPLY, hold_back, is_silent

FACTS = say('{"facts": []}')


def test_only_exactly_no_reply_is_silent():
    assert is_silent('NO_REPLY') and is_silent('  NO_REPLY\n') and NO_REPLY == 'NO_REPLY'
    for answer in ('', None, 'NO_REPLY.', 'Sent. NO_REPLY', 'no_reply', 'NO_REPLY\nSent 1 item.'):
        assert not is_silent(answer), answer


def collector():
    seen = []

    async def callback(event_type, data):
        seen.append((event_type, data.get('content', data.get('tool'))))
    return seen, callback


async def deltas(held, *texts):
    for index, text in enumerate(texts):
        await held('content_delta', {'content': text, 'index': index})


async def test_text_that_becomes_no_reply_never_goes_out():
    seen, callback = collector()
    held = hold_back(callback)
    await deltas(held, '\nNO', '_RE', 'PLY', '\n')
    assert seen == []
    await held.finish('NO_REPLY')
    assert seen == []


async def test_other_text_goes_out_in_order_as_soon_as_it_cant_be_no_reply():
    seen, callback = collector()
    held = hold_back(callback)
    await deltas(held, 'NO', ' problem', ', done')
    assert seen == [('content_delta', 'NO'), ('content_delta', ' problem'), ('content_delta', ', done')]
    await held.finish('NO problem, done')
    assert len(seen) == 3


async def test_a_tool_call_lets_waiting_text_through_first_and_finish_sends_the_rest():
    seen, callback = collector()
    held = hold_back(callback)
    await deltas(held, 'NO')
    await held('tool_call_start', {'tool': 'notify_owner'})
    await deltas(held, 'NO_RE')
    assert seen == [('content_delta', 'NO'), ('tool_call_start', 'notify_owner')]
    await held.finish('NO_RE')  # not silent: what waited is shown
    assert seen[-1] == ('content_delta', 'NO_RE')
    assert hold_back(None) is None


async def test_the_plain_terminal_prints_nothing_and_keeps_no_reply_out_of_the_history(make_runtime):
    model = ScriptedModel([[call('1', 'notify_owner', {'message': 'Sent 1 item.'})], [say('NO_REPLY')],
                           [say('Hello.')], [FACTS]])
    rt = make_runtime(model)
    owner = ScriptedTerminal('run news-feed', 'hi')
    conversation = await terminal.chat(rt, owner)
    assert 'NO_REPLY' not in owner.text and 'Hello.' in owner.text
    assert [(m.role, m.content) for m in rt.store.messages(conversation)] == [
        ('user', 'run news-feed'), ('user', 'hi'), ('assistant', 'Hello.')]
    assert 'NO_REPLY' not in str(model.inputs[2])  # the next run's history has no NO_REPLY either


async def test_a_normal_answer_prints_exactly_as_before(make_runtime):
    owner = ScriptedTerminal('hi')
    await terminal.chat(make_runtime(ScriptedModel([[say('NO problem.')], [FACTS]])), owner)
    assert owner.output[1:5] == ['NO p', 'robl', 'em.', '\n']  # streamed as it came, then the end of the line
