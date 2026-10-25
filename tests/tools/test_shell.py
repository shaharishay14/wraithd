"""
The shell: off by default; when on, every command asks y/N first, and it never runs unattended. Through the real
run() and the terminal chat; the commands that do run are harmless local ones (echo, pwd, sleep).
"""

import json

import pytest

from local_agent.channels import terminal
from local_agent.core import events as ev
from local_agent.core import gate as gate_module
from local_agent.core.gate import PermissionGate
from local_agent.store import base as store_base
from local_agent.tools.shell import shell_tool
from tests.scripted import ScriptedModel, ScriptedTerminal, call, of_type, run_once, say, unwrapped_json

SHELL_ON = '[shell]\nenabled = true\ntimeout_seconds = 1\n'


@pytest.fixture
def shell_on(home):
    home.config_path.write_text(SHELL_ON)
    return home


@pytest.fixture(autouse=True)
def quick_polls(monkeypatch):
    monkeypatch.setattr(gate_module, 'APPROVAL_POLL_SECONDS', 0.01)


def marker_turns(path):
    """The model asks to create a file: if it exists afterwards, the command ran."""
    return [[call('1', 'shell_run', {'command': f'echo ran > {path}'})], [say('Done.')]]


# --- off, unapproved, unattended ---------------------------------------------------------------------------

async def test_the_shell_is_off_by_default(make_runtime, home, events):
    model = ScriptedModel([[say('I cannot run commands.')]])
    await run_once(make_runtime(model), 'run ls', callback=events)
    assert 'shell_run' not in model.tool_names[0]
    # and the tool itself refuses when off, in case it's ever offered by mistake
    off = shell_tool(home.files_dir, 1, enabled=False)
    assert 'turned off' in json.loads(await off.on_invoke_tool(None, '{"command": "echo hi"}'))['error']


async def test_a_command_without_approval_does_not_run(make_runtime, shell_on, events):
    model = ScriptedModel(marker_turns('marker.txt'))
    rt = make_runtime(model)

    async def decline(event_type, data):
        await events(event_type, data)
        if event_type == 'approval_required':
            rt.store.decide_approval(data['approval_id'], store_base.DENIED)

    _, events_ = await run_once(rt, 'make a marker', callback=decline)
    card = next(data for event, data in events.recorded if event == 'approval_required')
    assert card['tool'] == 'shell_run' and card['arguments'] == {'command': 'echo ran > marker.txt'}
    assert 'with your permissions' in card['reason']
    assert not (shell_on.files_dir / 'marker.txt').exists()
    assert of_type(events_, ev.TOOL_RESULT)[0].payload['outcome'] == 'denied'


async def test_an_unanswered_approval_does_not_run_either(make_runtime, shell_on, events, fast_approvals):
    model = ScriptedModel(marker_turns('marker.txt'))
    await run_once(make_runtime(model), 'make a marker', callback=events)  # nobody answers
    assert 'did not approve' in model.tool_outputs(1)['call_1']
    assert not (shell_on.files_dir / 'marker.txt').exists()


async def test_the_shell_never_runs_unattended(make_runtime, shell_on, store):
    model = ScriptedModel([[say('A schedule cannot run commands.')]])
    await run_once(make_runtime(model), 'run the backup', source='schedule')  # no callback: unattended
    assert 'shell_run' not in model.tool_names[0]
    # even pre-approved, the gate refuses it with nobody there
    gate = PermissionGate(store, callback=None, unattended_write_tools=['shell_run'])
    assert 'never runs unattended' in json.loads(await gate.check('shell_run', {'command': 'ls'}))['error']


# --- approved ----------------------------------------------------------------------------------------------

async def test_yes_in_the_terminal_runs_the_command(make_runtime, shell_on):
    model = ScriptedModel([[call('1', 'shell_run', {'command': 'echo hello && pwd'})], [say('It printed hello.')],
                           [say('{"facts": []}')]])
    owner = ScriptedTerminal('say hello from the shell', 'y')
    await terminal.chat(make_runtime(model), owner)
    assert 'The agent wants to run shell_run:\n  command: echo hello && pwd' in owner.text
    output = model.tool_outputs(1)['call_1']
    # the output is third-party text (a command can print anything), and it ran in workspace/files
    assert output.startswith('<untrusted_tool_output tool="shell_run">')
    result = unwrapped_json(output)
    assert result['exit_code'] == 0 and result['stdout'].splitlines() == ['hello', str(shell_on.files_dir.resolve())]


async def test_a_slow_command_is_killed_at_the_timeout(home):
    tool = shell_tool(home.files_dir, 1, enabled=True)
    result = json.loads(await tool.on_invoke_tool(None, '{"command": "sleep 5; echo late"}'))
    assert 'did not finish within 1 seconds' in result['error']


async def test_commands_do_not_see_the_owners_keys(home, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'sk-should-not-leak-1234567890')
    tool = shell_tool(home.files_dir, 5, enabled=True)
    result = json.loads(await tool.on_invoke_tool(None, '{"command": "echo key=[$OPENAI_API_KEY]"}'))
    assert result['stdout'].strip() == 'key=[]'


async def test_output_is_capped_and_failures_count_as_errors(home):
    tool = shell_tool(home.files_dir, 5, enabled=True)
    result = json.loads(await tool.on_invoke_tool(None, json.dumps({'command': 'yes x | head -c 20000; exit 3'})))
    assert result['exit_code'] == 3 and 'exited with code 3' in result['error']
    assert len(result['stdout']) < 10_100 and 'more chars' in result['stdout']
