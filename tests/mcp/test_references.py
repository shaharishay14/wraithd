"""
1Password references for an MCP server's secrets (mcp/config.py): `--env KEY=op://vault/item/field` keeps the
reference in config.toml (it names a secret, it isn't one) and nothing in the keychain; the value is read with
`op read` on each connect and registered for redaction. The same arguments work from /mcp_add.
"""

import subprocess
import sys
import tomllib
from pathlib import Path

import keyring
import pytest

from wraithd import cli
from wraithd.config import load_config
from wraithd.core import secrets
from wraithd.mcp import config as mcp_config
from wraithd.mcp.config import McpConfigError, McpServer

FAKE = str(Path(__file__).parent.parent / 'fixtures' / 'fake_mcp.py')
REFERENCE = 'op://wraithd/Notes/credential'
VALUE = 'notes-value-from-1password-0123'


@pytest.fixture
def op(monkeypatch):
    """A 1Password CLI that knows one reference; records what it was asked."""
    asked = []

    def run(argv, **kwargs):
        asked.append(argv)
        known = argv[-1] == REFERENCE
        return subprocess.CompletedProcess(argv, 0 if known else 1, VALUE if known else '',
                                           '' if known else '[ERROR] could not find item')

    monkeypatch.setattr(mcp_config.shutil, 'which', lambda name: '/usr/local/bin/op' if name == 'op' else None)
    monkeypatch.setattr(mcp_config.subprocess, 'run', run)
    return asked


def test_the_cli_keeps_a_reference_in_the_config_and_nothing_in_the_keychain(home, isolated, capsys):
    assert cli.main(['mcp', 'add', 'notes', '--env', f'NOTES_TOKEN={REFERENCE}', '--',
                     sys.executable, FAKE, '--app', 'notes']) == 0
    [server] = load_config(home).mcp_servers
    assert server.env == ('NOTES_TOKEN',) and server.refs == (('NOTES_TOKEN', REFERENCE),)
    assert REFERENCE in home.config_path.read_text()
    assert keyring.get_password(mcp_config.APP, 'mcp/notes/env/NOTES_TOKEN') is None


def test_a_reference_is_read_with_op_on_connect_and_redacted(op):
    secrets.clear_registered()
    server = McpServer('notes', 'stdio', command='x', env=('NOTES_TOKEN',), refs=(('NOTES_TOKEN', REFERENCE),))
    env, _ = mcp_config.get_secrets(server)
    assert env == {'NOTES_TOKEN': VALUE} and op == [['/usr/local/bin/op', 'read', '--no-newline', REFERENCE]]
    assert secrets.redact_text(f'token {VALUE}') == 'token [REDACTED:owner_secret]'


def test_a_reference_op_cannot_read_is_a_clear_error(op, monkeypatch):
    server = McpServer('notes', 'stdio', command='x', env=('T',), refs=(('T', 'op://wraithd/Missing/field'),))
    with pytest.raises(McpConfigError, match='op could not read op://wraithd/Missing/field: .*could not find'):
        mcp_config.get_secrets(server)
    monkeypatch.setattr(mcp_config.shutil, 'which', lambda name: None)
    with pytest.raises(McpConfigError, match='1Password CLI \\(op\\) isn\'t installed'):
        mcp_config.get_secrets(server)


@pytest.mark.parametrize('refs', ['{OTHER = "op://a/b/c"}', '{T = "plain-value"}', '"op://a/b/c"'])
def test_refs_in_the_file_must_be_references_to_the_servers_names(refs):
    data = tomllib.loads(f'[mcp.servers.notes]\ncommand = "x"\nenv = ["T"]\nrefs = {refs}\n')
    with pytest.raises(McpConfigError, match='refs'):
        mcp_config.parse_servers(data)


def test_the_block_round_trips():
    server = McpServer('docs', 'http', url='https://docs.example/mcp', headers=('Authorization',),
                       refs=(('Authorization', REFERENCE),))
    [parsed] = mcp_config.parse_servers(tomllib.loads(mcp_config.render_block(server)))
    assert parsed == server
