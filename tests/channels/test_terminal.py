"""
The terminal chat, end to end with the scripted model: a preference said in one session holds in the next,
approvals are a y/N question, and every run can be shown afterwards with `local-agent runs show`.
"""

import json

from local_agent import cli
from local_agent.channels import terminal
from local_agent.core import events as ev
from local_agent.core import gate as gate_module
from local_agent.run import run
from local_agent.store import base as store_base
from tests.scripted import ScriptedModel, ScriptedTerminal, call, say

DISTILLED = {'facts': [{'path': 'profile/profile.md', 'title': 'Profile', 'text': '- Prefers short replies',
                        'evidence': 'I prefer short replies'}]}


async def test_a_preference_said_in_one_session_holds_in_the_next(make_runtime, home, capsys):
    # Session 1: the owner states a preference and quits. Distill turns it into the profile.
    first = ScriptedModel([[say('Got it, I will keep it short.')], [say(json.dumps(DISTILLED))]])
    owner = ScriptedTerminal('I prefer short replies', '/quit')
    await terminal.chat(make_runtime(first), owner)
    assert 'Got it, I will keep it short.' in owner.text and 'Remembered: profile/profile.md' in owner.text
    assert '- Prefers short replies' in (home.memory_dir / 'profile' / 'profile.md').read_text()
    # That happened between runs: `local-agent events` shows it
    capsys.readouterr()
    assert cli.main(['events']) == 0
    [line] = capsys.readouterr().out.splitlines()
    assert ' distill ' in line and line.endswith('into memory: profile/profile.md')

    # Session 2, a new process: the preference is in the first run's context, as the profile (L0)
    second = ScriptedModel([[say('Paris.')], [say('{"facts": []}')]])
    rt = make_runtime(second)
    await terminal.chat(rt, ScriptedTerminal('Capital of France?'))
    assert 'Profile:\n- Prefers short replies' in second.instructions[0]

    # ...and `local-agent runs show` prints that run's timeline
    [latest, *_] = rt.store.recent_runs()
    assert latest.message == 'Capital of France?'
    capsys.readouterr()
    assert cli.main(['runs', 'show', latest.run_id[:8]]) == 0
    shown = capsys.readouterr().out
    assert shown.startswith(f'Run {latest.run_id}') and 'answered · 1 model calls · 0 tool calls' in shown
    assert '#0   run_start      source=chat message=Capital of France?' in shown
    assert 'context_built  model=scripted disclosure=direct' in shown and 'run_end' in shown


async def test_the_conversation_carries_over_between_turns(make_runtime):
    model = ScriptedModel([[say('Hi Dana.')], [say('You are Dana.')], [say('{"facts": []}')]])
    await terminal.chat(make_runtime(model), ScriptedTerminal('I am Dana', 'who am I?', 'exit'))
    second_input = model.inputs[1]
    assert [item['content'] for item in second_input if isinstance(item, dict) and 'role' in item
            and '<system-reminder>' not in str(item['content'])] == ['I am Dana', 'Hi Dana.', 'who am I?']


async def test_a_write_asks_y_or_n_and_no_means_no(make_runtime, monkeypatch):
    monkeypatch.setattr(gate_module, 'APPROVAL_POLL_SECONDS', 0.01)
    model = ScriptedModel([[call('1', 'skill_adjust', {'skill': 'weekly-review', 'rule': 'Keep it to 5 lines'})],
                           [say('Not saved.')], [say('{"facts": []}')]])
    rt = make_runtime(model)
    owner = ScriptedTerminal('keep weekly reviews to 5 lines, always', 'n')
    await terminal.chat(rt, owner)
    assert 'The agent wants to run skill_adjust:' in owner.text and '  rule: Keep it to 5 lines' in owner.text
    assert owner.prompts[1] == 'Allow? [y/N] '
    [row] = rt.store._query('SELECT status FROM approvals')
    assert row['status'] == store_base.DENIED
    assert 'The user declined' not in model.tool_outputs(1)['call_1']  # skill_adjust has its own message
    assert 'Still do it their way' in model.tool_outputs(1)['call_1']


async def test_a_failed_run_is_shown_and_the_session_goes_on(make_runtime):
    model = ScriptedModel([RuntimeError('a'), RuntimeError('b'), RuntimeError('c'), [say('Back.')],
                           [say('{"facts": []}')]])
    owner = ScriptedTerminal('hi', 'hi again')
    rt = make_runtime(model)
    await terminal.chat(rt, owner)
    assert 'Nothing was changed. Please try again.' in owner.text and 'Back.' in owner.text
    outcomes = [rt.store.run_events(run.run_id)[-1].payload['outcome'] for run in rt.store.recent_runs()]
    assert sorted(outcomes) == ['answered', 'failed']


def test_runs_list_and_show_without_any_runs(capsys):
    assert cli.main(['runs', 'list']) == 0 and 'No runs yet.' in capsys.readouterr().out
    assert cli.main(['runs', 'show', 'abc']) == 1 and "No run matches 'abc'" in capsys.readouterr().err
    assert cli.main(['events']) == 0 and 'Nothing has happened between runs yet.' in capsys.readouterr().out


async def test_runs_show_as_json_and_ambiguous_prefixes(make_runtime, capsys):
    rt = make_runtime(ScriptedModel([[say('one')], [say('two')]]))
    for message in ('first', 'second'):
        await run(rt, message)
    capsys.readouterr()
    assert cli.main(['runs', 'list']) == 0
    listed = capsys.readouterr().out.splitlines()
    assert len(listed) == 2 and listed[0].endswith('chat      second')
    assert cli.main(['runs', 'show', '']) == 1
    run_id = rt.store.recent_runs()[0].run_id
    assert cli.main(['runs', 'show', run_id, '--json']) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown['summary']['outcome'] == 'answered' and shown['events'][0]['type'] == ev.RUN_START


def test_chat_explains_a_missing_key_instead_of_crashing(capsys):
    assert cli.main(['chat']) == 1
    assert 'local-agent key set openai' in capsys.readouterr().err


def test_key_set_stores_the_key_in_the_keychain(monkeypatch, isolated, capsys):
    monkeypatch.setattr(cli.getpass, 'getpass', lambda prompt: '  sk-from-the-prompt-123  ')
    assert cli.main(['key', 'set', 'anthropic']) == 0
    assert isolated.passwords[('local-agent', 'anthropic')] == 'sk-from-the-prompt-123'
    assert 'sk-from' not in capsys.readouterr().out
