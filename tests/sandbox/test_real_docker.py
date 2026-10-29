"""
The sandboxed shell against a real Docker. Skipped unless LOCAL_AGENT_DOCKER_TESTS=1 (it pulls an image and needs
the Docker daemon); run with:

    LOCAL_AGENT_DOCKER_TESTS=1 uv run pytest tests/sandbox/test_real_docker.py

Its state folder is made in the repo (or LOCAL_AGENT_DOCKER_TMP) and removed after.
"""

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from local_agent import sandbox
from local_agent.config import HOME_ENV, Home, load_config
from local_agent.tools.shell import shell_tool

pytestmark = pytest.mark.skipif(os.environ.get('LOCAL_AGENT_DOCKER_TESTS') != '1' or not shutil.which('docker'),
                                reason='real Docker test: set LOCAL_AGENT_DOCKER_TESTS=1 (needs docker)')
DOCKER_CONFIG = os.environ.get('DOCKER_CONFIG') or os.path.expanduser('~/.docker')  # before the tests swap HOME


@pytest.fixture(autouse=True)
def docker_context(monkeypatch):
    monkeypatch.setenv('DOCKER_CONFIG', DOCKER_CONFIG)  # so the client finds Docker Desktop's socket


@pytest.fixture
def home(monkeypatch):
    """A state folder Docker Desktop can mount: it doesn't share pytest's /private/var/folders temp folders."""
    base = os.environ.get('LOCAL_AGENT_DOCKER_TMP') or str(Path(__file__).resolve().parents[2])
    root = Path(tempfile.mkdtemp(prefix='.docker-test-', dir=base))
    monkeypatch.setenv(HOME_ENV, str(root))
    home = Home.locate()
    home.ensure()
    yield home
    shutil.rmtree(root, ignore_errors=True)


async def invoke(home, command, timeout=60):
    home.config_path.write_text('[shell]\nenabled = true\n\n[sandbox]\nbackend = "docker"\n')
    tool = shell_tool(home.files_dir, timeout, enabled=True, sandbox=sandbox.backend_for(load_config(home), home))
    return json.loads(await tool.on_invoke_tool(None, json.dumps({'command': command})))


async def test_the_container_sees_only_workspace_files_and_no_network(home):
    (home.files_dir / 'note.txt').write_text('hello from files\n')
    result = await invoke(home, 'pwd; cat note.txt; echo made > out.txt')
    assert result['exit_code'] == 0, result
    assert result['stdout'].splitlines() == ['/workspace', 'hello from files']
    assert (home.files_dir / 'out.txt').read_text() == 'made\n'  # written back, as the owner
    for escape in ('cat ../../config.toml', f'ls {home.root}', 'ls /workspace/..'):
        result = await invoke(home, escape)
        assert 'config.toml' not in result['stdout'] and 'state.db' not in result['stdout']
    (home.files_dir / 'link').symlink_to(home.root)
    assert (await invoke(home, 'cat link/config.toml'))['exit_code'] != 0
    result = await invoke(home, 'cat /sys/class/net/*/operstate 2>/dev/null; ls /sys/class/net')
    assert result['stdout'].split() in (['down', 'lo'], ['unknown', 'lo'], ['lo'])


async def test_a_timeout_stops_the_container(home):
    result = await invoke(home, 'sleep 120', timeout=3)
    assert 'did not finish within 3 seconds' in result['error']
