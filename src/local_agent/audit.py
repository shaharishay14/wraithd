"""
`local-agent audit`: the live setup checked against the security rules (docs/ec2-to-laptop.md). One line per rule,
pass or FAIL with a fix, and a few info lines for standing permissions worth knowing about. Exit code 1 on a FAIL.

Like doctor it only reads (the config, file modes, the store) and never changes anything. doctor runs it too and
reports one line for all of it.
"""

import contextlib
import ipaddress
import os
import stat
from pathlib import Path
from urllib.parse import urlsplit

from local_agent.config import ConfigError, Home, load_config
from local_agent.core import gate
from local_agent.core.secrets import redact
from local_agent.daemon import app as daemon_app
from local_agent.daemon.security import TOKEN_FILE
from local_agent.doctor import FAIL, INFO, OK, Check, render

MIN_TOKEN_LENGTH = 32
MAX_LISTED = 3  # offending paths named per rule
DB_FILES = ('', '-wal', '-shm')  # state.db and SQLite's side files: the event log, the review queue, the pins
TAILNET = '.ts.net'


def _shown(home: Home, paths: list[Path]) -> str:
    names = [str(path.relative_to(home.root)) for path in paths[:MAX_LISTED]]
    return ', '.join(names) + (f' and {len(paths) - MAX_LISTED} more' if len(paths) > MAX_LISTED else '')


def _loose(home: Home) -> tuple[list[Path], list[Path]]:
    """Paths inside the state folder that others can write to, and paths another account owns. Symlinks aren't
    followed: what they point to isn't the folder's."""
    writable, foreign = [], []
    uid = os.getuid()
    for folder, folders, files in os.walk(home.root):
        for name in folders + files:
            path = Path(folder) / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                continue
            if info.st_mode & 0o022:
                writable.append(path)
            if info.st_uid != uid:
                foreign.append(path)
    return writable, foreign


def _local_origin(origin: str) -> bool:
    """http on localhost or 127.0.0.1 with a real port, and nothing else (config only checks the prefix)."""
    parts = urlsplit(origin)
    try:
        port = parts.port
    except ValueError:
        return False
    return (parts.scheme == 'http' and parts.hostname in ('localhost', '127.0.0.1') and port is not None
            and not parts.path.strip('/') and not parts.username)


def checks(home: Home) -> list[Check]:
    found = []

    def add(state: str, text: str, fix: str = '') -> None:
        found.append(Check(state, text, fix))

    # --- the state folder: memory, the event log, the token, the config -------------------------------------
    if not home.root.is_dir():
        add(FAIL, f'No state folder at {home.root}', 'local-agent onboard')
        return found
    info = home.root.stat()
    mode = stat.S_IMODE(info.st_mode)
    add(OK if mode & 0o077 == 0 and info.st_uid == os.getuid() else FAIL,
        f'{home.root} is yours alone (mode {mode:o})', f'chmod 700 {home.root}')
    writable, foreign = _loose(home)
    add(OK if not writable else FAIL,
        'Nothing in it is writable by other accounts' if not writable else f'Writable by other accounts: '
        f'{_shown(home, writable)}', f'chmod -R go-w {home.root}')
    add(OK if not foreign else FAIL,
        'Everything in it belongs to you' if not foreign else f'Owned by another account: {_shown(home, foreign)}',
        f'chown -R {os.getuid()} {home.root}')

    # --- the local API ----------------------------------------------------------------------------------------
    loopback = ipaddress.ip_address(daemon_app.HOST).is_loopback
    add(OK if loopback else FAIL, f'The API binds to {daemon_app.HOST} only' if loopback else
        f'The API binds to {daemon_app.HOST}, which other machines can reach', 'reinstall Local Agent')
    token = home.root / TOKEN_FILE
    if token.exists():
        mode = stat.S_IMODE(token.stat().st_mode)
        strong = len(token.read_text().strip()) >= MIN_TOKEN_LENGTH
        add(OK if mode & 0o077 == 0 and strong else FAIL,
            f'API token is set and private (mode {mode:o})' if strong else 'API token is too short',
            f'chmod 600 {token}' if strong else f'rm {token} and restart the daemon (it makes a new one)')
    else:
        add(OK, 'API token: not made yet (the daemon makes one, mode 600, before it serves anything)')

    try:
        config = load_config(home)
    except ConfigError as e:
        add(FAIL, f'Settings don\'t load, so the rules below can\'t be checked: {e}', f'edit {home.config_path}')
        return found

    if not config.dev_origin:
        add(OK, 'Only the daemon\'s own origin may call the API')
    elif _local_origin(config.dev_origin):
        add(INFO, f'One more origin may call the API (with the token): {config.dev_origin}')
    else:
        add(FAIL, f'dev_origin {config.dev_origin} isn\'t on this machine: that site\'s pages may call the API',
            f'remove dev_origin from [daemon] in {home.config_path}')

    # --- the shell --------------------------------------------------------------------------------------------
    gated = gate.classify_tool('shell_run') == gate.ToolAccess.WRITE and 'shell_run' in gate.NEVER_UNATTENDED
    if not config.shell_enabled:
        add(OK, 'Shell: off')
    else:
        add(OK if gated else FAIL, 'Shell: on, every command asks first and never runs unattended' if gated else
            'Shell: on, and not every command asks first', f'set shell_enabled = false in {home.config_path}')

    # --- keys -------------------------------------------------------------------------------------------------
    _, kinds = redact(home.config_path.read_text()) if home.config_path.exists() else ('', [])
    add(OK if not kinds else FAIL, 'No keys in config.toml (they belong in the keychain)' if not kinds else
        f'config.toml holds what looks like a key ({", ".join(kinds)})',
        f'remove it from {home.config_path}; keep it with local-agent key set <provider> or mcp add --env')

    # --- standing permissions: not problems, but the owner should know they exist -----------------------------
    allowed = [f'{server.name} {tool}' for server in config.mcp_servers for tool in server.always_allow]
    if allowed:
        add(INFO, f'Always allowed (run without asking, but only to recipients you wrote): {", ".join(allowed)}. '
                  'A tool the server marks destructive asks anyway.')
    if home.db_path.exists():
        from local_agent.store import SQLiteStore

        store = SQLiteStore(home.db_path)
        try:
            active = len(store.list_schedules())
        finally:
            store.close()
        if active:
            add(INFO, f'{active} schedule{"s" if active > 1 else ""} run{"" if active > 1 else "s"} unattended: '
                      'local-agent schedules list')
    # New rules: each worker adds `found += its_rules(home, config)` (a list of Check) inside its block only
    # >>> security
    found += security_rules(home, config)
    # <<< security
    # >>> efficiency
    # <<< efficiency
    # >>> capabilities
    # <<< capabilities
    # >>> reach
    # <<< reach
    # >>> evals
    # <<< evals
    # >>> sandbox
    from local_agent.sandbox.audit import sandbox_rules

    found += sandbox_rules(home, config)
    # <<< sandbox
    return found


def remote_hosts(config) -> list[str]:
    """The [remote] allowed_hosts entries (reach's daemon.security.remote_hosts; none until it exists)."""
    from local_agent.daemon import security

    found = getattr(security, 'remote_hosts', None)
    return list(found(config)) if found else []


def _remote_host_problem(host: str) -> str:
    """Why a remote host isn't a tailnet name, or '' if it is one."""
    name = host.strip().lower().rstrip('.')
    with contextlib.suppress(ValueError):
        ipaddress.ip_address(name.strip('[]'))
        return 'is an IP address'
    if name in ('localhost', '127.0.0.1') or name.endswith('.localhost'):
        return 'is this machine'
    if not name.endswith(TAILNET) or name == TAILNET.lstrip('.'):
        return f'isn\'t a tailnet name (*{TAILNET})'
    return ''


def security_rules(home: Home, config) -> list[Check]:
    """The review queue's and the pins' storage, pinned MCP tools, remote access (docs/security.md)."""
    found = []

    def add(state: str, text: str, fix: str = '') -> None:
        found.append(Check(state, text, fix))

    # --- state.db holds the review queue (third-party text) and the pins: the owner's alone ----------------------
    exposed = [Path(f'{home.db_path}{suffix}') for suffix in DB_FILES]
    exposed = [path for path in exposed if path.exists() and stat.S_IMODE(path.stat().st_mode) & 0o077]
    add(OK if not exposed else FAIL, 'The store (review queue, pins, event log) is readable by you alone' if not exposed
        else f'Other accounts can open the store: {_shown(home, exposed)}',
        f'chmod 600 {" ".join(str(path) for path in exposed)}')

    # --- always-allowed MCP tools are pinned to what they were when allowed --------------------------------------
    allowed = [(server.name, tool) for server in config.mcp_servers for tool in server.always_allow]
    rows = {}
    if home.db_path.exists():
        from local_agent.mcp.pinning import Pins
        from local_agent.review.queue import ReviewQueue
        from local_agent.store import SQLiteStore

        store = SQLiteStore(home.db_path)
        try:
            rows = {(row['server'], row['tool']): row for row in Pins(store).rows()}
            pending = ReviewQueue(store).pending_count()
        finally:
            store.close()
        if pending:
            add(INFO, f'{pending} item{"s" if pending > 1 else ""} wait{"" if pending > 1 else "s"} for your review: '
                      'local-agent review list')
    unpinned = [f'{server} {tool}' for server, tool in allowed
                if not (rows.get((server, tool)) or {}).get('allowed_hash')]
    changed = [f'{server} {tool}' for server, tool in allowed
               if (row := rows.get((server, tool))) and row['allowed_hash'] and row['allowed_hash'] != row['seen_hash']]
    if allowed:
        add(OK if not unpinned else FAIL, 'Always-allowed MCP tools are pinned to what you allowed' if not unpinned
            else f'Always allowed but not pinned, so they ask (re-allow needed): {", ".join(unpinned)}',
            'local-agent mcp allow <server> <tool> for each (it checks the tool and pins it)')
        add(OK if not changed else FAIL, 'No always-allowed MCP tool changed since you allowed it' if not changed
            else f'Changed since you allowed them, so they ask until you review: {", ".join(changed)}',
            'local-agent review list')

    # --- remote access: only through the tailnet, and still with the token -------------------------------------
    for host in remote_hosts(config):
        problem = _remote_host_problem(host)
        if problem:
            add(FAIL, f'Remote access host {host} {problem}: anyone who can reach it could reach the API',
                'use your tailnet name from `tailscale status`')
        else:
            add(INFO, f'Reachable remotely at https://{host.strip().rstrip(".")} (token still required)')
    return found


def main(home: Home) -> int:
    found = checks(home)
    print(render(found))
    return 1 if any(check.state == FAIL for check in found) else 0
