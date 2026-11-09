"""
Keys never reach a process wraithd starts: the shell, an MCP stdio server, Claude Code. Every 1Password variable
(OP_*) counts as a key: OP_SERVICE_ACCOUNT_TOKEN unlocks the vault the other keys came from. The one exception is
`op read` itself (mcp/config.py op_read), which needs the token.

And keys can come from systemd's encrypted credentials ($CREDENTIALS_DIRECTORY), before the keychain and the
environment (docs/keys.md).

Each subprocess path is checked at the point the process is really started: the env handed to the spawn call.
"""

import json
import subprocess
import sys

import pytest

from wraithd import config
from wraithd.core import secrets
from wraithd.mcp import client as mcp_client
from wraithd.mcp import config as mcp_config
from wraithd.mcp.config import McpServer
from wraithd.tools.shell import shell_tool

OP_TOKEN = 'ops_eyJvcGVuIjoic2VzYW1lIn0-service-account-token'
KEYS = {'OP_SERVICE_ACCOUNT_TOKEN': OP_TOKEN, 'OP_SESSION_myteam': 'op-session-value-123456',
        'OP_CONNECT_TOKEN': 'op-connect-token-value-1234', 'OPENAI_API_KEY': 'sk-should-not-leak-1234567890',
        'WRAITHD_MCP_OTHER_TOKEN': 'another-servers-token-12345'}


@pytest.fixture
def keys_in_environment(monkeypatch):
    for name, value in KEYS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('HARMLESS_SETTING', 'kept')
    yield KEYS
    secrets.clear_registered()


def test_every_op_variable_is_a_secret():
    for name in ('OP_SERVICE_ACCOUNT_TOKEN', 'OP_SESSION_myteam', 'OP_CONNECT_TOKEN', 'OP_CONNECT_HOST'):
        assert secrets.is_secret_variable(name)
    assert not secrets.is_secret_variable('OPTIONS') and not secrets.is_secret_variable('PATH')


def test_an_op_token_in_a_result_is_redacted(keys_in_environment):
    text, found = secrets.redact(f'the env said OP_SERVICE_ACCOUNT_TOKEN={OP_TOKEN}')
    assert OP_TOKEN not in text and 'owner_secret' in found


async def test_the_shell_gets_no_keys(home, keys_in_environment):
    tool = shell_tool(home.files_dir, 5, enabled=True)
    result = json.loads(await tool.on_invoke_tool(None, '{"command": "env"}'))
    names = {line.split('=', 1)[0] for line in result['stdout'].splitlines()}
    assert not names & set(KEYS) and 'HARMLESS_SETTING' in names
    assert not [value for value in KEYS.values() if value in result['stdout']]


async def test_an_mcp_stdio_server_gets_only_its_own_values(keys_in_environment, monkeypatch):
    import mcp.client.stdio as stdio

    started = {}

    async def spawn(command, args, env=None, errlog=None, cwd=None):
        started['env'] = env
        raise RuntimeError('captured, not started')

    monkeypatch.setattr(stdio, '_create_platform_compatible_process', spawn)
    monkeypatch.setenv('WRAITHD_MCP_NOTES_NOTES_TOKEN', 'notes-own-token-123456')
    server = mcp_client.sdk_server(McpServer(name='notes', transport='stdio', command=sys.executable,
                                             args=('-c', 'pass'), env=('NOTES_TOKEN',)))
    with pytest.raises(Exception):  # noqa: B017: whatever the SDK wraps it in, the spawn was attempted
        await server.connect()
    await server.cleanup()

    env = started['env']
    assert env['NOTES_TOKEN'] == 'notes-own-token-123456'
    assert not set(env) & set(KEYS) and not [value for value in KEYS.values() if value in env.values()]


async def test_claude_code_gets_no_keys(keys_in_environment, monkeypatch):
    """Through claude-agent-sdk's own transport: it starts Claude Code with the whole environment and options.env
    over it, so every key has to be blanked there."""
    pytest.importorskip('claude_agent_sdk')
    from claude_agent_sdk._internal.transport import subprocess_cli

    from wraithd.engines.claude_options import claude_options

    started = {}

    async def spawn(cmd, **kwargs):
        started['env'] = kwargs['env']
        raise RuntimeError('captured, not started')

    monkeypatch.setenv('CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK', '1')
    monkeypatch.setattr(subprocess_cli.anyio, 'open_process', spawn)
    options = claude_options('', None, [], 'claude-haiku-5-5')
    options.cli_path = sys.executable
    transport = subprocess_cli.SubprocessCLITransport(prompt='hi', options=options)
    with pytest.raises(Exception):  # noqa: B017
        await transport.connect()

    env = started['env']
    assert all(env.get(name) == '' for name in KEYS) and env['ANTHROPIC_API_KEY'] == ''
    assert not [value for value in KEYS.values() if value in env.values()]
    assert env['HARMLESS_SETTING'] == 'kept'


def test_op_read_keeps_the_token_for_op_itself(keys_in_environment, monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout='the-value-from-1password', stderr='')

    monkeypatch.setattr(mcp_config.shutil, 'which', lambda name: '/usr/bin/op')
    monkeypatch.setattr(mcp_config.subprocess, 'run', run)
    assert mcp_config.op_read('op://vault/item/field') == 'the-value-from-1password'
    assert calls and 'env' not in calls[0]  # op inherits wraithd's environment, token and all


# --- systemd credentials ---------------------------------------------------------------------------------------

@pytest.fixture
def credentials(tmp_path, monkeypatch):
    folder = tmp_path / 'credentials'
    folder.mkdir()
    monkeypatch.setenv(secrets.CREDENTIALS_ENV, str(folder))
    yield folder
    secrets.clear_registered()


def test_a_key_in_the_credentials_folder_comes_first(credentials, monkeypatch, isolated):
    (credentials / 'OPENAI_API_KEY').write_text('sk-from-systemd-credential-1234\n')  # echo's newline dropped
    monkeypatch.setenv('OPENAI_API_KEY', 'sk-from-the-environment-123456')
    isolated.set_password(config.APP, 'openai', 'sk-from-the-keychain-1234567')
    assert config.get_key('openai') == 'sk-from-systemd-credential-1234'
    assert '[REDACTED' in secrets.redact_text('key: sk-from-systemd-credential-1234')


def test_a_credential_named_like_the_provider_works_too(credentials):
    (credentials / 'telegram').write_text('123456:telegram-bot-token-value')
    assert config.get_key('telegram') == '123456:telegram-bot-token-value'
    assert secrets.redact_text('token 123456:telegram-bot-token-value') == 'token [REDACTED:owner_secret]'


def test_without_a_credential_the_keychain_and_environment_still_work(credentials, monkeypatch, isolated):
    monkeypatch.setenv('SERPAPI_KEY', 'serpapi-from-the-environment')
    isolated.set_password(config.APP, 'openai', 'sk-from-the-keychain-1234567')
    assert config.get_key('serpapi') == 'serpapi-from-the-environment'
    assert config.get_key('openai') == 'sk-from-the-keychain-1234567'
    assert config.get_key('anthropic') is None


def test_no_credentials_folder_reads_nothing(monkeypatch, tmp_path):
    monkeypatch.delenv(secrets.CREDENTIALS_ENV, raising=False)
    assert secrets.credential('OPENAI_API_KEY') is None


def test_a_credential_name_is_never_a_path(credentials, tmp_path):
    (tmp_path / 'outside').write_text('not-a-credential-value')
    (credentials / 'sub').mkdir()
    assert secrets.credential('../outside', '/etc/passwd', '..', 'sub', '') is None


def test_an_mcp_value_comes_from_the_credentials_folder(credentials, isolated):
    (credentials / 'WRAITHD_MCP_NOTES_NOTES_TOKEN').write_text('notes-token-from-systemd')
    isolated.set_password(config.APP, 'mcp:notes:env:NOTES_TOKEN', 'notes-token-from-keychain')
    env, _ = mcp_config.get_secrets(McpServer(name='notes', transport='stdio', command='x', env=('NOTES_TOKEN',)))
    assert env == {'NOTES_TOKEN': 'notes-token-from-systemd'}
    assert secrets.redact_text('notes-token-from-systemd') == '[REDACTED:owner_secret]'
