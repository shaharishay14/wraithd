"""
MCP servers in config.toml: one block per server, written by `wraithd mcp add`. Secrets (env values for a
local server, header values for a remote one) are never in the file: the block names them, and the values are in
the OS keychain under that server's name, so each one goes only to its own server.

    [mcp.servers.gmail]
    transport = "stdio"
    command = "npx"
    args = ["-y", "some-gmail-mcp"]
    env = ["GMAIL_TOKEN"]            # names only
    provides = ["email"]             # what skills can count on once it's connected
    always_allow = ["send_email"]    # tools that run without asking (wraithd mcp allow)
    tools = ["fetch_emails"]         # optional: only these tools are offered; the rest are never shown or run
    sandbox = true                   # optional, stdio only: runs in docker (wraithd mcp add --sandbox)
    refs = {GMAIL_TOKEN = "op://wraithd/Gmail/token"}  # optional: a 1Password reference instead of a stored value

A reference isn't a secret (it names where the secret is), so it can live in the file; the value is read with
`op read` each time the server connects, by the daemon's own 1Password CLI login (docs/keys.md).

The blocks are generated, so the CLI rewrites a server's block whole; the rest of the file (and its comments) is
left as it is.
"""

import contextlib
import itertools
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path

import keyring
import keyring.errors

from wraithd import rename
from wraithd.core import secrets

APP = 'wraithd'  # the keychain service, as in config.py
NAME = re.compile(r'^[a-z][a-z0-9-]{0,30}$')
CAPABILITY = re.compile(r'^[a-z][a-z0-9-]{0,30}$')
TRANSPORTS = ('stdio', 'http')
REFERENCE = re.compile(r'^op://[^/\s]+/[^/\s]+/\S+$')  # a 1Password secret reference: op://vault/item/field
OP_TIMEOUT_SECONDS = 30


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
    sandbox: bool = False  # a stdio server that runs inside docker (docs/sandbox.md)
    tools: tuple[str, ...] = ()  # the only tools offered (wraithd mcp add --tools); empty: every tool it lists
    refs: tuple[tuple[str, str], ...] = ()  # (env or header name, op:// reference): read with `op read` on connect

    def offers(self, tool: str) -> bool:
        """The owner's allow-list lets this tool through (no list: every tool does)."""
        return not self.tools or tool in self.tools


def _strings(value, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise McpConfigError(f'{where} must be a list of strings.')
    return tuple(value)


def is_reference(value: str) -> bool:
    """A 1Password secret reference (op://vault/item/field): where a secret is, not the secret."""
    return bool(REFERENCE.match(value or ''))


def _refs(value, where: str, names: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise McpConfigError(f'{where} must be a table of names to op:// references.')
    for key, reference in value.items():
        if key not in names or not is_reference(reference):
            raise McpConfigError(f'{where} {key}: each entry is one of the server\'s env or header names, with an '
                                 'op://vault/item/field reference.')
    return tuple(value.items())


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
                           always_allow=_strings(table.get('always_allow'), f'{where} always_allow'),
                           sandbox=table.get('sandbox', False),
                           tools=_strings(table.get('tools'), f'{where} tools'))
        server = replace(server, refs=_refs(table.get('refs'), f'{where} refs', server.env + server.headers))
        if not isinstance(server.sandbox, bool) or server.sandbox and transport != 'stdio':
            raise McpConfigError(f'{where} sandbox must be true or false, and only a local (stdio) server has one.')
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
    try:
        keyring.set_password(APP, _account(server, kind, key), value)
    except keyring.errors.NoKeyringError as e:
        raise McpConfigError(f'This machine has no keychain, so {key} can\'t be saved. Give it to wraithd as the '
                             f'environment variable {env_name(server, key)} instead: see docs/keys.md.') from e


def env_name(server: str, key: str) -> str:
    """The environment variable a value is read from when it isn't in the keychain: WRAITHD_MCP_X_API_KEY.
    The prefix is the server's, so a value still goes only to its own server (and the shell never sees it)."""
    return re.sub(r'[^A-Z0-9]+', '_', f'{secrets.MCP_ENV_PREFIX}{server}_{key}'.upper())


def _keychain(account: str) -> str | None:
    try:
        value = keyring.get_password(APP, account)
    except keyring.errors.NoKeyringError:  # a headless server: the environment is the only source
        return None
    return value or rename.old_keychain_get(account)


def op_read(reference: str) -> str:
    """A 1Password reference's value, through the 1Password CLI (`op read`). McpConfigError when it can't be read;
    the message never has the value in it."""
    op = shutil.which('op')
    if op is None:
        raise McpConfigError(f'{reference} is a 1Password reference, but the 1Password CLI (op) isn\'t installed on '
                             'this machine: see docs/keys.md.')
    try:
        done = subprocess.run([op, 'read', '--no-newline', reference], capture_output=True, text=True,
                              timeout=OP_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise McpConfigError(f'Reading {reference} with op failed ({type(e).__name__}).') from None
    if done.returncode != 0 or not done.stdout:
        why = secrets.redact_text(done.stderr.strip().splitlines()[-1]) if (done.stderr or '').strip() else ''
        raise McpConfigError(f'op could not read {reference}' + (f': {why[:200]}' if why else '') +
                             '. Is the daemon started with a 1Password login (OP_SERVICE_ACCOUNT_TOKEN)?')
    return done.stdout


def get_secrets(server: McpServer) -> tuple[dict[str, str], dict[str, str]]:
    """(env, headers) for this server only, from the keychain or else the environment (env_name). Every value is
    registered for redaction."""
    found, refs = {'env': {}, 'header': {}}, dict(server.refs)
    for kind, names in (('env', server.env), ('header', server.headers)):
        for key in names:
            if key in refs:
                found[kind][key] = value = op_read(refs[key])
                secrets.register_secret(value)
                continue
            name = env_name(server.name, key)
            value = _keychain(_account(server.name, kind, key)) or os.environ.get(name) or rename.old_mcp_env(name)
            if value is None:
                raise McpConfigError(f'The {kind} value {key} for {server.name} is missing: it\'s not in the keychain '
                                     f'and {env_name(server.name, key)} isn\'t set. Add the server again '
                                     f'(wraithd mcp add {server.name} ...) or set that variable.')
            secrets.register_secret(value)
            found[kind][key] = value
    return found['env'], found['header']


def delete_secrets(server: McpServer) -> None:
    for kind, names in (('env', server.env), ('header', server.headers)):
        for key, service in itertools.product(names, (APP, rename.OLD_APP)):  # and any left under the old name
            with contextlib.suppress(keyring.errors.PasswordDeleteError, keyring.errors.NoKeyringError):
                keyring.delete_password(service, _account(server.name, kind, key))


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
    if server.tools:
        lines.append(f'tools = {_toml_list(server.tools)}    # only these tools are offered')
    if server.sandbox:
        lines.append('sandbox = true                   # runs inside docker ([sandbox] image and network)')
    if server.refs:
        lines.append('refs = {' + ', '.join(f'{json.dumps(k)} = {json.dumps(v)}' for k, v in server.refs)
                     + '}    # 1Password references, read with op on connect')
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
    for tool in server.tools:
        if not tool.strip():
            raise McpConfigError('A tool name in --tools is empty.')
    return replace(server, provides=tuple(dict.fromkeys(server.provides)), tools=tuple(dict.fromkeys(server.tools)))
