"""
The chat's rich look, played through prompt_toolkit's pipe input: answers as Markdown, the approval card with its
picker (Deny by default, y still allows), tool lines, the footer, /commands, history and multi-line input.
"""

import asyncio

from tests.cli.conftest import CTRL_D, DOWN, ENTER, ESC_ENTER, TAB, UP
from tests.scripted import ScriptedModel, call, say
from wraithd.channels import terminal
from wraithd.core import gate as gate_module
from wraithd.store import base as store_base

ADJUST = call('1', 'skill_adjust', {'skill': 'weekly-review', 'rule': 'Keep it to 5 lines'})
FACTS = say('{"facts": []}')


async def chat(rt, term):
    return await asyncio.wait_for(terminal.chat(rt, term), 10)


async def typed(term, chunks):
    """Keys with a pause between chunks, like a person: the completer runs in the background."""
    for chunk in chunks:
        await asyncio.sleep(0.2)
        term.pipe.send_text(chunk)


async def test_an_answer_is_markdown_with_a_header_and_a_footer(make_runtime, rich):
    model = ScriptedModel([[say('# Plan\n\n- **one**\n- two\n\n```\nprint(1)\n```')], [FACTS]])
    with rich('hello' + ENTER + CTRL_D) as term:
        await chat(make_runtime(model), term)
    out = term.out.getvalue()
    assert out.startswith('◆ wraithd  openai · gpt-4o-mini  ')
    assert '• one' in out and '• two' in out and '**' not in out and '# Plan' not in out and 'Plan' in out
    assert '│ print(1)' in out and '```' not in out
    assert out.rstrip().endswith('s') and 's\n' in out  # the footer: time (and tokens, when the model reports them)


async def test_an_approval_is_a_card_and_deny_is_the_default(make_runtime, rich, monkeypatch):
    monkeypatch.setattr(gate_module, 'APPROVAL_POLL_SECONDS', 0.01)
    model = ScriptedModel([[ADJUST], [say('Not saved.')], [FACTS]])
    rt = make_runtime(model)
    with rich('keep weekly reviews to 5 lines' + ENTER + ENTER + CTRL_D) as term:
        await chat(rt, term)
    out = term.out.getvalue()
    assert '╭─ The agent wants to run skill_adjust' in out and 'rule   Keep it to 5 lines' in out
    assert '✓ Allow? Deny' in out and '✗ skill_adjust  denied by you' in out
    [row] = rt.store._query('SELECT status FROM approvals')
    assert row['status'] == store_base.DENIED


async def test_y_still_allows_and_the_tool_line_says_who_decided(make_runtime, rich, monkeypatch):
    monkeypatch.setattr(gate_module, 'APPROVAL_POLL_SECONDS', 0.01)
    model = ScriptedModel([[ADJUST], [say('Saved.')], [FACTS]])
    rt = make_runtime(model)
    with rich('keep weekly reviews short' + ENTER + 'y' + ENTER + CTRL_D) as term:
        await chat(rt, term)
    out = term.out.getvalue()
    assert '✓ Allow? Allow once' in out and '✓ skill_adjust  approved by you' in out and 'Saved.' in out
    [row] = rt.store._query('SELECT status FROM approvals')
    assert row['status'] == store_base.APPROVED


async def test_a_tool_error_is_a_cross_and_a_good_result_a_tick(make_runtime, rich):
    model = ScriptedModel([[call('1', 'schedule_cancel', {'id': 99})], [call('2', 'schedule_list', {})],
                           [say('No such schedule.')], [FACTS]])
    with rich('cancel schedule 99' + ENTER + CTRL_D) as term:
        await chat(make_runtime(model), term)
    out = term.out.getvalue()
    assert '✗ schedule_cancel  allowed · error' in out and '✓ schedule_cancel' not in out
    assert '✓ schedule_list  allowed' in out and 'No such schedule.' in out


async def test_warnings_about_strangers_are_at_the_top_of_the_card(rich):
    from wraithd.tui.chat import approval_card

    card = {'tool': 'send_email', 'arguments': {'to': 'eve@evil.example', 'body': 'hi'},
            'suspicious_recipients': [{'value': 'eve@evil.example', 'source': 'read_email'}]}
    with rich() as term:
        term.console.print(approval_card(card))
    lines = term.out.getvalue().splitlines()
    assert 'send_email' in lines[0]
    assert '! eve@evil.example was not written by you (it came from read_email)' in lines[1]
    assert lines.index(next(line for line in lines if 'to    eve@evil.example' in line)) > 1


async def test_slash_commands_tab_completion_and_history(make_runtime, rich):
    model = ScriptedModel([[say('First.')], [say('First again.')], [FACTS]])
    rt = make_runtime(model)
    keys = ['/he', TAB, ENTER + '/runs' + ENTER + '/bogus' + ENTER + '/model' + ENTER
            + 'hello' + ENTER, UP + UP + DOWN, ENTER + CTRL_D]  # ↑↑↓: back to the last one
    with rich() as term:
        await asyncio.gather(chat(rt, term), typed(term, keys))
    out = term.out.getvalue()
    assert '/schedules' in out and 'Esc Enter' in out  # /help, completed from /he
    assert 'No runs yet.' in out
    assert "/bogus isn't a command." in out and 'openai · gpt-4o-mini  change it with `wraithd onboard`' in out
    assert [m.message for m in rt.store.recent_runs()] == ['hello', 'hello']  # ↑ brought back the last message
    assert '/help' not in str(model.inputs)  # commands never reach the model


async def test_esc_enter_is_a_new_line_in_the_same_message(make_runtime, rich):
    model = ScriptedModel([[say('Ok.')], [FACTS]])
    rt = make_runtime(model)
    with rich('line one' + ESC_ENTER + 'line two' + ENTER + CTRL_D) as term:
        await chat(rt, term)
    assert [m.message for m in rt.store.recent_runs()] == ['line one\nline two']


async def test_new_starts_a_fresh_conversation(make_runtime, rich):
    model = ScriptedModel([[say('Hi.')], [FACTS], [say('Hello again.')], [FACTS]])
    rt = make_runtime(model)
    with rich('hi' + ENTER + '/new' + ENTER + 'hello' + ENTER + '/quit' + ENTER) as term:
        await chat(rt, term)
    assert 'New conversation.' in term.out.getvalue()
    assert len(rt.store._query('SELECT id FROM conversations')) == 2
    second = model.inputs[2]
    assert 'hi' not in [item.get('content') for item in second if isinstance(item, dict)]


async def test_quitting_closes_the_warm_sessions_in_the_rich_look_too(make_runtime, rich, monkeypatch):
    rt = make_runtime(ScriptedModel([]))
    closed = []

    async def close_sessions():
        closed.append(True)

    monkeypatch.setattr(rt, 'close_sessions', close_sessions)
    for keys in ('/quit' + ENTER, CTRL_D):
        with rich(keys) as term:
            await chat(rt, term)
    assert closed == [True, True]
