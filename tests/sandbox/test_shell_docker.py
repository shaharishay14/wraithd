"""
The shell with [sandbox] backend = "docker": the docker command line it builds (only workspace/files mounted, no
network), the same timeout, output cap and key-free environment, and the refusal when the setting is broken. A fake
docker records the argv and runs nothing.
"""

import json
import os

import pytest

from local_agent import sandbox
from local_agent.config import load_config
from local_agent.core import events as ev
from local_agent.core import gate as gate_module
from local_agent.core.gate import PermissionGate
from local_agent.sandbox import docker
from local_agent.sandbox.docker import Result
from local_agent.store import base as store_base
from local_agent.tools.shell import shell_tool
from tests.sandbox.fakes import FakeDocker, flag_values
from tests.scripted import ScriptedModel, call, of_type, run_once, say, unwrapped_json

DOCKER_ON = '[shell]\nenabled = true\ntimeout_seconds = 3\n\n[sandbox]\nbackend = "docker"\n'


@pytest.fixture(autouse=True)
def quick_polls(monkeypatch):
    monkeypatch.setattr(gate_module, 'APPROVAL_POLL_SECONDS', 0.01)


@pytest.fixture
def fake(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(docker, 'run_exec', fake)
    return fake


def docker_shell(home, fake, settings=DOCKER_ON, timeout=3):
    home.config_path.write_text(settings)
    return shell_tool(home.files_dir, timeout, enabled=True, sandbox=sandbox.backend_for(load_config(home), home, fake))


async def invoke(tool, command):
    return json.loads(await tool.on_invoke_tool(None, json.dumps({'command': command})))


def test_backend_none_is_the_default_and_keeps_todays_shell(home):
    assert load_config(home).section('sandbox') == {'backend': 'none', 'image': 'debian:bookworm-slim',
                                                    'network': 'none'}
    assert sandbox.backend_for(load_config(home), home) is None


async def test_docker_mounts_only_workspace_files_with_no_network(home, fake):
    result = await invoke(docker_shell(home, fake), 'ls -la && cat notes.txt')
    assert result == {'exit_code': 0, 'stdout': 'ran in the fake\n', 'stderr': ''}
    argv = fake.argv
    assert argv[:3] == ['docker', 'run', '--rm']
    assert flag_values(argv, '--network') == ['none']
    assert flag_values(argv, '--mount') == [f'type=bind,source={os.path.realpath(home.files_dir)},target=/workspace']
    assert '-v' not in argv and '--volume' not in argv and '--privileged' not in argv
    assert flag_values(argv, '--workdir') == ['/workspace']
    assert flag_values(argv, '--cap-drop') == ['ALL'] and '--read-only' in argv
    assert flag_values(argv, '--user') == [f'{os.getuid()}:{os.getgid()}']
    # the command is one argument to sh -c inside the container: nothing on this machine parses it
    assert argv[-4:] == ['debian:bookworm-slim', 'sh', '-c', 'ls -la && cat notes.txt']


async def test_the_docker_client_gets_no_keys_and_the_same_timeout(home, fake, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'sk-should-not-leak-1234567890')
    await invoke(docker_shell(home, fake, timeout=7), 'echo hi')
    assert 'OPENAI_API_KEY' not in fake.calls[0]['env'] and fake.calls[0]['timeout'] == 7
    assert not any('sk-should-not-leak' in item for item in fake.argv)


async def test_a_timeout_kills_the_container_by_name(home, monkeypatch):
    fake = FakeDocker(Result(None, b'', b'', timed_out=True), Result(0, b'', b''))
    result = await invoke(docker_shell(home, fake), 'sleep 600')
    assert 'did not finish within 3 seconds' in result['error']
    name = flag_values(fake.calls[0]['argv'], '--name')[0]
    assert name.startswith('local-agent-shell-')
    assert fake.calls[1]['argv'] == ['docker', 'kill', name]


async def test_output_is_capped_and_a_nonzero_exit_is_an_error(home):
    fake = FakeDocker(Result(3, b'x' * 20_000, b'oops'))
    result = await invoke(docker_shell(home, fake), 'false')
    assert result['exit_code'] == 3 and 'exited with code 3' in result['error']
    assert len(result['stdout']) < 10_100 and 'more chars' in result['stdout'] and result['stderr'] == 'oops'


async def test_a_broken_setting_refuses_and_never_runs_unsandboxed(home, fake):
    for settings in ('[sandbox]\nbackend = "bubblewrap"\n', '[sandbox]\nbackend = "docker"\nnetwork = "host"\n',
                     '[sandbox]\nbackend = "docker"\nimage = "--privileged"\n'):
        tool = docker_shell(home, fake, settings)
        marker = home.files_dir / 'ran.txt'
        result = await invoke(tool, f'echo ran > {marker}')
        assert 'set up wrong, so nothing runs' in result['error'] and 'local-agent audit' in result['error']
        assert not marker.exists()
    assert fake.calls == []


async def test_missing_docker_is_a_clear_error(home):
    async def no_docker(argv, env, timeout):
        raise FileNotFoundError('docker')

    result = await invoke(docker_shell(home, no_docker), 'ls')
    assert 'Docker is not installed' in result['error']


def test_the_tool_says_where_it_runs(home, fake):
    assert 'sandbox that sees only' in docker_shell(home, fake).description
    assert 'on the user\'s computer' in shell_tool(home.files_dir, 3, enabled=True).description


async def test_the_bridge_network_is_passed_as_set(home, fake):
    await invoke(docker_shell(home, fake, DOCKER_ON + 'network = "bridge"\n'), 'ls')
    assert flag_values(fake.argv, '--network') == ['bridge']


# --- through a real run ------------------------------------------------------------------------------------

async def test_an_approved_command_runs_in_docker_and_is_an_event(make_runtime, home, fake, events):
    home.config_path.write_text(DOCKER_ON)
    model = ScriptedModel([[call('1', 'shell_run', {'command': 'cat ../../config.toml'})], [say('Done.')]])
    rt = make_runtime(model)

    async def approve(event_type, data):
        await events(event_type, data)
        if event_type == 'approval_required':
            rt.store.decide_approval(data['approval_id'], store_base.APPROVED)

    _, saved = await run_once(rt, 'read the config', callback=approve)
    assert unwrapped_json(model.tool_outputs(1)['call_1'])['stdout'] == 'ran in the fake\n'
    [event] = of_type(saved, ev.SANDBOX_RUN)
    assert event.payload == {'backend': 'docker', 'image': 'debian:bookworm-slim', 'network': 'none',
                             'mounts': [os.path.realpath(home.files_dir)], 'container': event.payload['container'],
                             'exit_code': 0, 'timed_out': False}
    assert flag_values(fake.argv, '--network') == ['none']


async def test_an_unapproved_command_never_reaches_docker(make_runtime, home, fake, fast_approvals):
    home.config_path.write_text(DOCKER_ON)
    model = ScriptedModel([[call('1', 'shell_run', {'command': 'ls'})], [say('Done.')]])
    await run_once(make_runtime(model), 'list', callback=lambda *_: _noop())  # nobody answers
    assert fake.calls == []


async def test_a_broken_sandbox_is_an_event_in_the_run(make_runtime, home, fake):
    home.config_path.write_text('[shell]\nenabled = true\n\n[sandbox]\nbackend = "podman"\n')
    model = ScriptedModel([[call('1', 'shell_run', {'command': 'ls'})], [say('Done.')]])
    rt = make_runtime(model)

    async def approve(event_type, data):
        if event_type == 'approval_required':
            rt.store.decide_approval(data['approval_id'], store_base.APPROVED)

    _, saved = await run_once(rt, 'list', callback=approve)
    [event] = of_type(saved, ev.SANDBOX_REFUSED)
    assert "backend 'podman'" in event.payload['problems'][0] and fake.calls == []


async def test_sandboxed_or_not_the_shell_never_runs_unattended(make_runtime, home, fake, store):
    home.config_path.write_text(DOCKER_ON)
    model = ScriptedModel([[say('A schedule cannot run commands.')]])
    await run_once(make_runtime(model), 'run the backup', source='schedule')
    assert 'shell_run' not in model.tool_names[0]
    gate = PermissionGate(store, callback=None, unattended_write_tools=['shell_run'])
    assert 'never runs unattended' in json.loads(await gate.check('shell_run', {'command': 'ls'}))['error']
    assert fake.calls == []


async def _noop():
    return None
