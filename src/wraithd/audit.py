"""
`wraithd audit`: the live setup checked against the security rules (docs/ec2-to-laptop.md). One line per rule,
pass or FAIL with a fix, and a few info lines for standing permissions worth knowing about. Exit code 1 on a FAIL.

Like doctor it only reads (the config, file modes, the store) and never changes anything. doctor runs it too and
reports one line for all of it.
"""

import contextlib
import ipaddress
import os
import re
import stat
from pathlib import Path
from urllib.parse import urlsplit

from wraithd.config import ConfigError, Home, load_config
from wraithd.core import gate, secrets
from wraithd.core.secrets import redact
from wraithd.daemon import app as daemon_app
from wraithd.daemon.security import TOKEN_FILE
from wraithd.doctor import (
    CLAUDE_ENGINE,
    CLAUDE_EXTRA,
    FAIL,
    INFO,
    OK,
    WARN,
    Check,
    engine_name,
    engine_settings,
    render,
)

MIN_TOKEN_LENGTH = 32
MAX_LISTED = 3  # offending paths named per rule
DB_FILES = ('', '-wal', '-shm')  # state.db and SQLite's side files: the event log, the review queue, the pins
TAILNET = '.ts.net'
# A Claude login (OAuth) token, as `claude setup-token` prints it. wraithd must never hold one
CLAUDE_TOKEN = re.compile(r'sk-ant-oat\d*-[A-Za-z0-9_-]{10,}')


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
        add(FAIL, f'No state folder at {home.root}', 'wraithd onboard')
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
        f'The API binds to {daemon_app.HOST}, which other machines can reach', 'reinstall wraithd')
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
        f'remove it from {home.config_path}; keep it with wraithd key set <provider> or mcp add --env')

    # --- reach: what the agent can open on its own (docs/lockdown.md) -------------------------------------------
    from wraithd.tools import choose_search

    browser = config.section('browser')['enabled']
    add(INFO, f'Web: search {choose_search(config).backend or "off"}, page reading '
              f'{"on" if config.web_read else "off"}, browser {"on" if browser else "off"}')
    for server in config.mcp_servers:
        add(INFO, f'MCP {server.name}: ' + (f'only {", ".join(server.tools)}' if server.tools else
                                            'every tool it lists (no tools list)'))

    # --- standing permissions: not problems, but the owner should know they exist -----------------------------
    allowed = [f'{server.name} {tool}' for server in config.mcp_servers for tool in server.always_allow]
    if allowed:
        add(INFO, f'Always allowed (run without asking, but only to recipients you wrote): {", ".join(allowed)}. '
                  'A tool the server marks destructive asks anyway.')
    if home.db_path.exists():
        from wraithd.store import SQLiteStore

        store = SQLiteStore(home.db_path)
        try:
            active = len(store.list_schedules())
        finally:
            store.close()
        if active:
            add(INFO, f'{active} schedule{"s" if active > 1 else ""} run{"" if active > 1 else "s"} unattended: '
                      'wraithd schedules list')
    # New rules: each worker adds `found += its_rules(home, config)` (a list of Check) inside its block only
    # >>> security
    found += security_rules(home, config)
    found += subscription_rules(home, config)  # subscription mode
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
    from wraithd.sandbox.audit import sandbox_rules

    found += sandbox_rules(home, config)
    # <<< sandbox
    return found


def remote_hosts(config) -> list[str]:
    """The [remote] allowed_hosts entries (reach's daemon.security.remote_hosts; none until it exists)."""
    from wraithd.daemon import security

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
        from wraithd.mcp.pinning import Pins
        from wraithd.review.queue import ReviewQueue
        from wraithd.store import SQLiteStore

        store = SQLiteStore(home.db_path)
        try:
            rows = {(row['server'], row['tool']): row for row in Pins(store).rows()}
            pending = ReviewQueue(store).pending_count()
        finally:
            store.close()
        if pending:
            add(INFO, f'{pending} item{"s" if pending > 1 else ""} wait{"" if pending > 1 else "s"} for your review: '
                      'wraithd review list')
    unpinned = [f'{server} {tool}' for server, tool in allowed
                if not (rows.get((server, tool)) or {}).get('allowed_hash')]
    changed = [f'{server} {tool}' for server, tool in allowed
               if (row := rows.get((server, tool))) and row['allowed_hash'] and row['allowed_hash'] != row['seen_hash']]
    if allowed:
        add(OK if not unpinned else FAIL, 'Always-allowed MCP tools are pinned to what you allowed' if not unpinned
            else f'Always allowed but not pinned, so they ask (re-allow needed): {", ".join(unpinned)}',
            'wraithd mcp allow <server> <tool> for each (it checks the tool and pins it)')
        add(OK if not changed else FAIL, 'No always-allowed MCP tool changed since you allowed it' if not changed
            else f'Changed since you allowed them, so they ask until you review: {", ".join(changed)}',
            'wraithd review list')

    # --- remote access: only through the tailnet, and still with the token -------------------------------------
    for host in remote_hosts(config):
        problem = _remote_host_problem(host)
        if problem:
            add(FAIL, f'Remote access host {host} {problem}: anyone who can reach it could reach the API',
                'use your tailnet name from `tailscale status`')
        else:
            add(INFO, f'Reachable remotely at https://{host.strip().rstrip(".")} (token still required)')
    return found


def _wraithd_keychain(config) -> list[tuple[str, str]]:
    """(account, value) for wraithd's own keychain entries: its keys and its MCP servers' values. Only wraithd's
    service names (and the old Local Agent one); never Claude Code's entry."""
    import keyring
    import keyring.errors

    from wraithd import rename
    from wraithd.config import APP, KEY_NAMES
    from wraithd.mcp.config import _account

    accounts = list(KEY_NAMES) + [_account(server.name, kind, key) for server in config.mcp_servers
                                  for kind, names in (('env', server.env), ('header', server.headers))
                                  for key in names]
    found = []
    for service in (APP, rename.OLD_APP):
        for account in accounts:
            try:
                value = keyring.get_password(service, account)
            except keyring.errors.NoKeyringError:
                return found
            if value:
                found.append((account, value))
    return found


def subscription_rules(home: Home, config) -> list[Check]:
    """The Claude subscription's rules (docs/claude-subscription.md). Looks only at wraithd's own config and keychain
    entries: never at ~/.claude or Claude Code's keychain entry."""
    found = []

    def add(state: str, text: str, fix: str = '') -> None:
        found.append(Check(state, text, fix))

    # --- wraithd never holds a Claude login token, whichever engine runs ----------------------------------------
    held = ['config.toml'] if home.config_path.exists() and CLAUDE_TOKEN.search(home.config_path.read_text()) else []
    held += [f'keychain {account}' for account, value in _wraithd_keychain(config) if CLAUDE_TOKEN.search(value)]
    add(OK if not held else FAIL, 'wraithd holds no Claude login token' if not held else
        f'wraithd holds a Claude login token ({", ".join(held)}): it must never have one',
        'remove it (wraithd key rm <name>, or edit the config); Claude Code keeps its own login')

    if engine_name(home) != CLAUDE_ENGINE:
        return found
    # --- Claude Code only gets wraithd's guarded tools, and none of the owner's other Claude Code settings --------
    try:
        from wraithd.engines.claude_code import SubscriptionUnavailable
        from wraithd.engines.claude_options import claude_options, model_for, subscription_env_problem
    except ImportError as e:  # the claude extra (claude-agent-sdk) isn't installed
        add(FAIL, f'Claude Code extra not installed ({e.name or e}), so the engine can\'t run', CLAUDE_EXTRA)
        return found
    # Claude Code is on the owner's own login, not Bedrock or Vertex (apiKeySource reads none there too)
    problem = subscription_env_problem()
    add(OK if not problem else FAIL, problem or 'Claude Code isn\'t sent to Bedrock or Vertex',
        'unset that variable where wraithd runs (or set it to 0)')
    try:
        options = claude_options('', None, [], model_for(engine_settings(home), 'chat'))
    except SubscriptionUnavailable as e:
        add(FAIL, f'The engine can\'t run: {e}', CLAUDE_EXTRA)
        return found
    env = dict(getattr(options, 'env', None) or {})
    loose = [name for name, safe in (
        ('its own tools are on (tools is not [])', getattr(options, 'tools', None) == []),
        ('other MCP servers may load (strict_mcp_config is off)', getattr(options, 'strict_mcp_config', None) is True),
        ('your Claude Code settings load (setting_sources is not [])', getattr(options, 'setting_sources', None) == []),
        ('it is handed a login token (CLAUDE_CODE_OAUTH_TOKEN)', 'CLAUDE_CODE_OAUTH_TOKEN' not in env),
        ('it inherits a key wraithd holds (a model key, an MCP value or an OP_* variable)',
         all(env.get(name) == '' for name in os.environ if secrets.is_secret_variable(name))),
    ) if not safe]
    add(OK if not loose else FAIL, 'Claude Code gets only wraithd\'s guarded tools, no other settings' if not loose
        else f'Claude Code isn\'t locked down: {"; ".join(loose)}', 'reinstall wraithd')
    if os.environ.get('ANTHROPIC_API_KEY'):
        add(WARN, 'ANTHROPIC_API_KEY is set: wraithd blanks it for Claude Code, but elsewhere it quietly wins over '
                  'your Claude login', 'unset ANTHROPIC_API_KEY where wraithd runs, unless you want the key')
    return found


def main(home: Home) -> int:
    found = checks(home)
    print(render(found))
    return 1 if any(check.state == FAIL for check in found) else 0
