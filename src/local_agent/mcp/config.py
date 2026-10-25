"""
MCP servers in config.toml: one block per server, written by `local-agent mcp add`. Secrets (env values for a
local server, header values for a remote one) are never in the file: the block names them, and the values are in
the OS keychain under that server's name, so each one goes only to its own server.

    [mcp.servers.gmail]
    transport = "stdio"
    command = "npx"
    args = ["-y", "some-gmail-mcp"]
    env = ["GMAIL_TOKEN"]            # names only
    provides = ["email"]             # what skills can count on once it's connected
    always_allow = ["send_email"]    # tools that run without asking (local-agent mcp allow)

The blocks are generated, so the CLI rewrites a server's block whole; the rest of the file (and its comments) is
left as it is.
"""

import contextlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path

import keyring
import keyring.errors

from local_agent.core import secrets

APP = 'local-agent'  # the keychain service, as in config.py
NAME = re.compile(r'^[a-z][a-z0-9-]{0,30}$')
CAPABILITY = re.compile(r'^[a-z][a-z0-9-]{0,30}$')
TRANSPORTS = ('stdio', 'http')


class McpConfigError(ValueError):
    """The server can't be added or used as configured. The message says what to fix."""


@dataclass(frozen=True)
class McpServer:
    name: str
    transport: str  # stdio | http
    command: str = ''
    args: tuple[str, ...] = ()
    env: tuple[str, ...] = ()  # names of secret env vars, values in the keychain
    url: str = ''
    headers: tuple[str, ...] = ()  # names of secret headers, values in the keychain
    provides: tuple[str, ...] = ()
    always_allow: tuple[str, ...] = field(default=())


def _strings(value, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise McpConfigError(f'{where} must be a list of strings.')
    return tuple(value)


def parse_servers(data: dict) -> tuple[McpServer, ...]:
    """The [mcp.servers.*] tables of a loaded config.toml."""
    servers = []
    for name, table in ((data.get('mcp') or {}).get('servers') or {}).items():
        where = f'[mcp.servers.{name}]'
        if not NAME.match(name) or not isinstance(table, dict):
            raise McpConfigError(f'{where}: server names are lowercase letters, digits and dashes.')
        transport = table.get('transport', 'stdio')
        if transport not in TRANSPORTS:
            raise McpConfigError(f'{where} transport must be "stdio" or "http".')
        server = McpServer(name=name, transport=transport, command=str(table.get('command') or ''),
                           args=_strings(table.get('args'), f'{where} args'),
                           env=_strings(table.get('env'), f'{where} env'),
                           url=str(table.get('url') or ''),
                           headers=_strings(table.get('headers'), f'{where} headers'),
                           provides=_strings(table.get('provides'), f'{where} provides'),
                           always_allow=_strings(table.get('always_allow'), f'{where} always_allow'))
        if transport == 'stdio' and not server.command:
            raise McpConfigError(f'{where} needs a command.')
        if transport == 'http' and not server.url.startswith(('http://', 'https://')):
            raise McpConfigError(f'{where} needs an http(s) url.')
        servers.append(server)
    return tuple(servers)


# --- secrets -----------------------------------------------------------------------------------------------

def _account(server: str, kind: str, key: str) -> str:
    return f'mcp/{server}/{kind}/{key}'


def set_secret(server: str, kind: str, key: str, value: str) -> None:
    keyring.set_password(APP, _account(server, kind, key), value)


def get_secrets(server: McpServer) -> tuple[dict[str, str], dict[str, str]]:
    """(env, headers) for this server only, from the keychain. Every value is registered for redaction."""
    found = {'env': {}, 'header': {}}
    for kind, names in (('env', server.env), ('header', server.headers)):
        for key in names:
            value = keyring.get_password(APP, _account(server.name, kind, key))
            if value is None:
                raise McpConfigError(f'The {kind} value {key} for {server.name} is missing from the keychain. Add the '
                                     f'server again: local-agent mcp add {server.name} ...')
            secrets.register_secret(value)
            found[kind][key] = value
    return found['env'], found['header']


def delete_secrets(server: McpServer) -> None:
    for kind, names in (('env', server.env), ('header', server.headers)):
        for key in names:
            with contextlib.suppress(keyring.errors.PasswordDeleteError):
                keyring.delete_password(APP, _account(server.name, kind, key))


# --- the file ----------------------------------------------------------------------------------------------

def _toml_list(values) -> str:
    return '[' + ', '.join(json.dumps(value) for value in values) + ']'


def render_block(server: McpServer) -> str:
    lines = [f'[mcp.servers.{server.name}]', f'transport = {json.dumps(server.transport)}']
    if server.transport == 'stdio':
        lines += [f'command = {json.dumps(server.command)}', f'args = {_toml_list(server.args)}']
        if server.env:
            lines.append(f'env = {_toml_list(server.env)}            # names only: the values are in the keychain')
    else:
        lines.append(f'url = {json.dumps(server.url)}')
        if server.headers:
            lines.append(f'headers = {_toml_list(server.headers)}    # names only: the values are in the keychain')
    lines.append(f'provides = {_toml_list(server.provides)}')
    lines.append(f'always_allow = {_toml_list(server.always_allow)}')
    return '\n'.join(lines) + '\n'


def _without_block(text: str, name: str) -> str:
    """The config text with this server's block removed (from its header to the next table header)."""
    lines, kept, skipping = text.splitlines(keepends=True), [], False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('['):
            skipping = stripped == f'[mcp.servers.{name}]'
        if not skipping:
            kept.append(line)
    return ''.join(kept)


def write_server(config_path: Path, server: McpServer | None, name: str) -> None:
    """Replaces (or with server=None removes) one server's block, atomically."""
    text = _without_block(config_path.read_text() if config_path.exists() else '', name).rstrip('\n')
    if server is not None:
        text = (text + '\n\n' if text else '') + render_block(server)
    handle, temporary = tempfile.mkstemp(dir=config_path.parent, prefix='.config.', suffix='.tmp')
    with os.fdopen(handle, 'w') as out:
        out.write(text if text.endswith('\n') else text + '\n')
    os.replace(temporary, config_path)


def validated(server: McpServer) -> McpServer:
    if not NAME.match(server.name):
        raise McpConfigError('Server names are lowercase letters, digits and dashes, e.g. "gmail" or "my-notes".')
    for capability in server.provides:
        if not CAPABILITY.match(capability):
            raise McpConfigError(f'{capability!r} is not a capability name, e.g. "email" or "calendar".')
    return replace(server, provides=tuple(dict.fromkeys(server.provides)))
