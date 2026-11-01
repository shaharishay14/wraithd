"""
A server with no keychain (headless Linux: keyring's fail backend raises on every call). Keys then come from the
environment, from whatever the owner uses to set it: an env file, a systemd EnvironmentFile, `op run`.
"""

import getpass
import json
import sys

import keyring
import pytest
from keyring.backends import fail

from wraithd import cli
from wraithd.config import KEY_ENV, NoKeychain, get_key, load_config, set_key
from wraithd.core import secrets
from wraithd.evals.runner import check_setup, parse_spec
from wraithd.mcp import config as mcp_config
from wraithd.tools.shell import shell_tool

OPENAI_KEY = 'sk-from-the-environment-0123456789'
X_TOKEN = 'x-token-from-the-environment-0123456789'


@pytest.fixture
def no_keychain(isolated):
    keyring.set_keyring(fail.Keyring())  # the isolated fixture puts the previous one back


def test_keys_come_from_the_environment(no_keychain, monkeypatch):
    assert get_key('openai') is None
    monkeypatch.setenv('OPENAI_API_KEY', OPENAI_KEY)
    assert get_key('openai') == OPENAI_KEY
    check_setup([parse_spec('openai:gpt-4o')], [], {})  # the eval runner finds it too


def test_saving_a_key_says_which_variable_to_set(no_keychain, capsys, monkeypatch):
    with pytest.raises(NoKeychain, match='OPENAI_API_KEY'):
        set_key('openai', OPENAI_KEY)
    monkeypatch.setattr(getpass, 'getpass', lambda prompt: OPENAI_KEY)
    assert cli.main(['key', 'set', 'openai']) == 1
    assert 'environment variable OPENAI_API_KEY' in capsys.readouterr().err


def test_an_mcp_value_from_the_environment_is_not_asked_for(no_keychain, home, monkeypatch, capsys):
    name = mcp_config.env_name('x', 'X_TOKEN')
    assert name == 'WRAITHD_MCP_X_X_TOKEN'
    monkeypatch.setenv(name, X_TOKEN)

    def ask(prompt):
        raise AssertionError('asked for a value the environment has')
    monkeypatch.setattr(getpass, 'getpass', ask)
    assert cli.main(['mcp', 'add', 'x', '--env', 'X_TOKEN', '--', sys.executable, '-c', 'pass']) == 0
    [server] = load_config(home).mcp_servers
    assert mcp_config.get_secrets(server) == ({'X_TOKEN': X_TOKEN}, {})
    assert cli.main(['mcp', 'add', 'x', '--env', 'X_TOKEN', '--', sys.executable, '-c', 'pass']) == 0  # replaced


def test_an_mcp_value_given_inline_says_which_variable_to_set(no_keychain, home, capsys):
    assert cli.main(['mcp', 'add', 'x', '--env', f'X_TOKEN={X_TOKEN}', '--', sys.executable, '-c', 'pass']) == 1
    assert 'WRAITHD_MCP_X_X_TOKEN' in capsys.readouterr().err
    assert not load_config(home).mcp_servers


def test_every_key_variable_counts_as_a_secret():
    assert set(KEY_ENV.values()) <= set(secrets.ENV_SECRETS)


async def test_the_shell_never_sees_a_key_from_the_environment(tmp_path, monkeypatch):
    values = {'ELEVENLABS_API_KEY': 'eleven-key-0123456789', 'OP_SERVICE_ACCOUNT_TOKEN': 'ops_token-0123456789',
              'WRAITHD_MCP_X_X_TOKEN': X_TOKEN, 'OPENAI_API_KEY': OPENAI_KEY}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('HARMLESS_SETTING', 'kept')
    shell = shell_tool(tmp_path, 5, enabled=True)
    printed = json.loads(await shell.on_invoke_tool(None, json.dumps({'command': 'env'})))['stdout']
    assert 'HARMLESS_SETTING=kept' in printed
    assert not any(value in printed for value in values.values())
    assert secrets.redact_text(f'echoed {X_TOKEN}') == 'echoed [REDACTED:owner_secret]'
