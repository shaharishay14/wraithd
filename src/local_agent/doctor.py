"""
`local-agent doctor`: is everything in place? One line per check, and a fix for each one that fails.

It reads (the config, the keychain, file modes, the service manager, /api/health) and never changes anything.
Keys are reported as present or missing, never shown. Exit code 1 if any check fails, so scripts can use it.
"""

import socket
import stat
import sys
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version

from local_agent import __version__
from local_agent.config import Config, ConfigError, Home, get_key, load_config
from local_agent.daemon import service as service_module
from local_agent.daemon.security import TOKEN_FILE
from local_agent.daemon.service import ServiceError

OK, FAIL, INFO = 'ok', 'FAIL', 'info'
NEEDS_KEY = {'openai', 'anthropic'}


@dataclass(frozen=True)
class Check:
    state: str  # OK, FAIL or INFO (worth knowing, not a problem)
    text: str
    fix: str = ''


def port_free(port: int) -> bool:
    """True if nothing listens on 127.0.0.1:port (binding is local; nothing is sent anywhere)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(('127.0.0.1', port))
        except OSError:
            return False
    return True


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _package(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return 'not installed'


def checks(home: Home, service=None, transport=None, port_free=port_free) -> list[Check]:
    found = []

    def add(state: str, text: str, fix: str = '') -> None:
        found.append(Check(state, text, fix))

    python = '.'.join(map(str, sys.version_info[:3]))
    add(OK if sys.version_info >= (3, 12) else FAIL, f'Python {python}', 'Local Agent needs Python 3.12 or newer.')
    add(INFO, f'local-agent {__version__} (openai-agents {_package("openai-agents")})')

    # --- the folder and the settings ---------------------------------------------------------------------------
    if not home.root.is_dir():
        add(FAIL, f'No state folder at {home.root}', 'local-agent onboard')
        return found
    mode = _mode(home.root)
    add(OK if mode & 0o077 == 0 else FAIL, f'{home.root} is private (mode {mode:o})',
        f'chmod 700 {home.root}')
    try:
        config = load_config(home)
        add(OK, f'Settings load: {home.config_path}')
    except ConfigError as e:
        add(FAIL, f'Settings don\'t load: {e}',
            f'edit {home.config_path}, or move it aside and run local-agent onboard')
        config = Config()

    # --- keys: present or not, never their values -------------------------------------------------------------
    provider = config.model.provider
    if provider in NEEDS_KEY:
        add(OK if get_key(provider) else FAIL, f'{provider} key: {"set" if get_key(provider) else "missing"}',
            f'local-agent key set {provider}')
    else:
        add(OK, f'{provider}: no key needed' + (f' ({config.model.base_url})' if config.model.base_url else ''))
    from local_agent.tools import choose_search

    search = choose_search(config)
    add(OK if search.backend else INFO, f'Web search: {search.why}'
        + (f' (local-agent key set {search.missing_key})' if search.missing_key else ''))
    add(INFO, 'Telegram bot token: ' + ('set' if get_key('telegram') else 'not set (Telegram is off)'))
    from local_agent.voice import choose_transcriber

    voice = choose_transcriber(config)
    add(OK if voice.name else INFO, f'Voice notes: {voice.why}')

    token = home.root / TOKEN_FILE
    if token.exists():
        mode = _mode(token)
        add(OK if mode & 0o077 == 0 else FAIL, f'API token file is private (mode {mode:o})', f'chmod 600 {token}')
    else:
        add(INFO, 'No API token yet: the daemon makes it when it first starts')

    from local_agent import audit  # it uses this module's Check

    failed = [check for check in audit.checks(home) if check.state == FAIL]
    add(OK if not failed else FAIL, 'Security rules: ' + ('all pass' if not failed else f'{len(failed)} fail'),
        'local-agent audit, for each rule and its fix')

    # --- the web UI -------------------------------------------------------------------------------------------
    from local_agent.daemon.app import ui_dir

    ui = ui_dir()
    add(OK if ui else FAIL, f'Web UI: {ui}' if ui else 'Web UI: not in this install (the daemon shows a placeholder)',
        'install a release build (scripts/release.sh builds one), or set LOCAL_AGENT_UI_DIR to a built frontend/dist')

    # --- the service and the daemon ---------------------------------------------------------------------------
    try:
        service = service or service_module.service_for(home)
        state = service.status()
        if not state.installed:
            add(FAIL, 'Background service: not installed', 'local-agent daemon install')
        else:
            add(OK, f'Background service: installed ({service.path})')
            add(OK if state.running else FAIL,
                'Background service: ' + (f'running (pid {state.pid})' if state.pid else 'running' if state.running
                                          else 'not running'),
                'local-agent daemon start (and local-agent daemon logs if it stops again)')
    except ServiceError as e:
        add(INFO, str(e))

    port = config.daemon_port
    answer = service_module.health(port, transport)
    if answer:
        same = answer.get('version') == __version__
        add(OK if same else FAIL, f'Daemon answers on port {port} (version {answer.get("version")})',
            'it is still running the old version: local-agent daemon restart')
    else:
        add(FAIL, f'Daemon: nothing answers on port {port}', 'local-agent daemon start (or run local-agent daemon)')
        free = port_free(port)
        add(OK if free else FAIL, f'Port {port}: ' + ('free' if free else 'in use by another program'),
            f'stop that program, or set another port in {home.config_path} ([daemon] port)')
    # New checks: each worker adds `found += its_checks(home, config)` (a list of Check) inside its block only
    # >>> security
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
    # <<< sandbox
    return found


def render(found: list[Check]) -> str:
    lines = []
    for check in found:
        lines.append(f'  {check.state:<5} {check.text}')
        if check.state == FAIL and check.fix:
            lines.append(f'        fix: {check.fix}')
    failed = sum(check.state == FAIL for check in found)
    lines.append('\nAll good.' if not failed else f'\n{failed} problem{"s" if failed > 1 else ""} found.')
    return '\n'.join(lines)


def main(home: Home, **kwargs) -> int:
    found = checks(home, **kwargs)
    print(render(found))
    return 1 if any(check.state == FAIL for check in found) else 0
