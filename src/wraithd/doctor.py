"""
`wraithd doctor`: is everything in place? One line per check, and a fix for each one that fails.

It reads (the config, the keychain, file modes, the service manager, /api/health) and never changes anything.
Keys are reported as present or missing, never shown. Exit code 1 if any check fails, so scripts can use it.
"""

import asyncio
import concurrent.futures
import contextlib
import re
import socket
import stat
import sys
import tomllib
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version

from wraithd import __version__, rename
from wraithd.config import Config, ConfigError, Home, get_key, load_config
from wraithd.daemon import service as service_module
from wraithd.daemon.security import TOKEN_FILE
from wraithd.daemon.service import ServiceError

OK, FAIL, INFO, WARN = 'ok', 'FAIL', 'info', 'warn'  # WARN: worth fixing, but nothing fails because of it
NEEDS_KEY = {'openai', 'anthropic'}
CLAUDE_ENGINE, DEFAULT_ENGINE = 'claude-code', 'agents-sdk'
LOGIN_OK = 'Claude subscription login in use'
SIGNED_OUT = 'Claude Code isn\'t signed in: run `claude` and type /login'
PLAN_LIMIT = 'Claude subscription login in use: signed in, but your plan limit is reached right now'
AUTH_WORDS = re.compile(r'log ?in|auth|api key|credential|unauthori[sz]ed|forbidden|token', re.IGNORECASE)
LOGIN_TIMEOUT = 60  # seconds for Claude Code to start and say which credential it chose
CLAUDE_EXTRA = "uv tool install 'wraithd[claude]'"


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


ENGINE_DEFAULTS = {'name': DEFAULT_ENGINE, 'chat_model': 'sonnet', 'background_model': 'haiku', 'fallback': False,
                   'idle_minutes': 10}


def engine_settings(home: Home) -> dict:
    """[engine] as config.toml has it, over the defaults. Read raw: it works whether or not [engine] is a registered
    section yet, and a broken file is the settings check's to report."""
    try:
        data = tomllib.loads(home.config_path.read_text()) if home.config_path.exists() else {}
    except (OSError, tomllib.TOMLDecodeError):
        data = {}
    table = data.get('engine', {})
    return {**ENGINE_DEFAULTS, **(table if isinstance(table, dict) else {})}


def engine_name(home: Home) -> str:
    name = engine_settings(home)['name']
    return name if isinstance(name, str) else DEFAULT_ENGINE


def _login_fix(source) -> str:
    if source == 'ANTHROPIC_API_KEY':
        return ('Claude Code is using ANTHROPIC_API_KEY, not your login: unset ANTHROPIC_API_KEY where wraithd runs, '
                'then run `claude` and type /login')
    if source == 'apiKeyHelper':
        return ('Claude Code is using an apiKeyHelper, not your login: remove apiKeyHelper from your Claude Code '
                'settings, then run `claude` and type /login')
    if source == '/login managed key':
        return ('Claude Code is signed in with an Anthropic Console account (API billing), not a Claude plan: run '
                '`claude`, type /logout, then /login and pick your Claude account')
    return f'Claude Code is using {source or "no login"}, not your Claude login: run `claude` and type /login'


async def claude_login_check(home: Home | None = None) -> tuple[bool, str]:
    """
    A tool-less one-shot on the background model (engines.claude_options.claude_options): which credential Claude
    Code chose (the init message's apiKeySource), then the result of one short reply, since apiKeySource reads none
    even when nobody is signed in. A login that isn't the subscription stops at the init, before any reply. Never
    opens Claude Code's credentials (the file or the keychain entry) and never sets CLAUDE_CODE_OAUTH_TOKEN: the
    login stays between the owner and Anthropic. Bedrock and Vertex are refused first (apiKeySource reads none there
    too).
    """
    try:
        from wraithd.engines.claude_code import SubscriptionUnavailable
        from wraithd.engines.claude_options import LOGIN, claude_options, model_for, subscription_env_problem
    except ImportError as e:  # the claude extra (claude-agent-sdk) isn't installed
        return False, f'Claude Code extra not installed ({e.name or e}): {CLAUDE_EXTRA}'
    problem = subscription_env_problem()
    if problem:
        return False, problem
    settings = engine_settings(home or Home.locate())
    try:
        options = claude_options('Reply with OK.', None, [], model_for(settings, 'heartbeat'))
    except SubscriptionUnavailable as e:
        return False, str(e)
    from claude_agent_sdk import ResultMessage, SystemMessage, query

    async def init_and_result() -> tuple[dict | None, object]:
        """The init message's data, then the ResultMessage of the one short reply (or None for either)."""
        init = None
        async with contextlib.aclosing(query(prompt='Reply with OK.', options=options)) as messages:
            async for message in messages:
                if isinstance(message, SystemMessage) and message.subtype == 'init':
                    init = dict(message.data)
                    if init.get('apiKeySource') != LOGIN:  # not the login: no need to spend a reply on it
                        return init, None
                elif isinstance(message, ResultMessage):
                    return init, message
        return init, None

    try:
        init, result = await asyncio.wait_for(init_and_result(), LOGIN_TIMEOUT)
    except TimeoutError:
        return False, f'Claude Code did not answer within {LOGIN_TIMEOUT} seconds'
    except Exception as e:  # the SDK's own errors: the program is missing, or it exited at once
        return False, f'Claude Code did not start ({type(e).__name__}: {e})'
    if init is None:
        return False, 'Claude Code did not start: is it installed?'
    source = init.get('apiKeySource')
    if source != LOGIN:
        return False, _login_fix(source)
    if result is None:
        return False, 'Claude Code started but never answered'
    return _result_check(result)


def _result_check(result) -> tuple[bool, str]:
    """The one reply's ResultMessage: signed in, signed out, the plan's limit, or another error."""
    if not getattr(result, 'is_error', False):
        return True, LOGIN_OK
    status = getattr(result, 'api_error_status', None)
    errors = getattr(result, 'errors', None) or []
    text = '; '.join(str(error) for error in errors) if isinstance(errors, list | tuple) else str(errors)
    text = text or str(getattr(result, 'result', '') or '')
    if status == 429:
        return True, PLAN_LIMIT
    if status in (401, 403) or AUTH_WORDS.search(text):
        return False, SIGNED_OUT
    detail = text or (f'HTTP {status}' if status else 'no details')
    return False, f'Claude Code answered with an error: {detail}'


def _wait(coroutine):
    """A coroutine's result from sync code, even when an event loop already runs in this thread."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        return pool.submit(asyncio.run, coroutine).result()


def subscription_checks(home: Home, login_check=None) -> list[Check]:
    """With [engine] name = "claude-code": is Claude Code on the owner's own login (docs/claude-subscription.md)."""
    if engine_name(home) != CLAUDE_ENGINE:
        return []
    ok, message = _wait((login_check or claude_login_check)(home))
    if message == PLAN_LIMIT:
        return [Check(WARN, message, 'wait for the limit to reset (claude.ai shows when), or set [engine] fallback')]
    return [Check(OK, message) if ok else Check(FAIL, message, 'see docs/claude-subscription.md')]


def checks(home: Home, service=None, transport=None, port_free=port_free, login_check=None) -> list[Check]:
    found = []

    def add(state: str, text: str, fix: str = '') -> None:
        found.append(Check(state, text, fix))

    python = '.'.join(map(str, sys.version_info[:3]))
    add(OK if sys.version_info >= (3, 12) else FAIL, f'Python {python}', 'wraithd needs Python 3.12 or newer.')
    add(INFO, f'wraithd {__version__} (openai-agents {_package("openai-agents")})')

    # --- the folder and the settings ---------------------------------------------------------------------------
    if not home.root.is_dir():
        add(FAIL, f'No state folder at {home.root}', 'wraithd onboard')
        return found
    if rename.both_homes(home.root):
        add(FAIL, f'The old Local Agent folder {rename.old_home()} is still there beside {home.root}',
            f'wraithd uses {home.root}; move anything you need from {rename.old_home()}, then delete it')
    mode = _mode(home.root)
    add(OK if mode & 0o077 == 0 else FAIL, f'{home.root} is private (mode {mode:o})',
        f'chmod 700 {home.root}')
    try:
        config = load_config(home)
        add(OK, f'Settings load: {home.config_path}')
    except ConfigError as e:
        add(FAIL, f'Settings don\'t load: {e}',
            f'edit {home.config_path}, or move it aside and run wraithd onboard')
        config = Config()

    # --- keys: present or not, never their values -------------------------------------------------------------
    provider = config.model.provider
    if engine_name(home) == CLAUDE_ENGINE:
        add(OK, 'Claude subscription: no API key needed (Claude Code uses your own login)')
    elif provider in NEEDS_KEY:
        add(OK if get_key(provider) else FAIL, f'{provider} key: {"set" if get_key(provider) else "missing"}',
            f'wraithd key set {provider}')
    else:
        add(OK, f'{provider}: no key needed' + (f' ({config.model.base_url})' if config.model.base_url else ''))
    from wraithd.tools import choose_search

    search = choose_search(config)
    add(OK if search.backend else INFO, f'Web search: {search.why}'
        + (f' (wraithd key set {search.missing_key})' if search.missing_key else ''))
    add(INFO, 'Telegram bot token: ' + ('set' if get_key('telegram') else 'not set (Telegram is off)'))
    from wraithd.voice import choose_transcriber

    voice = choose_transcriber(config)
    add(OK if voice.name else INFO, f'Voice notes: {voice.why}')

    token = home.root / TOKEN_FILE
    if token.exists():
        mode = _mode(token)
        add(OK if mode & 0o077 == 0 else FAIL, f'API token file is private (mode {mode:o})', f'chmod 600 {token}')
    else:
        add(INFO, 'No API token yet: the daemon makes it when it first starts')

    from wraithd import audit  # it uses this module's Check

    failed = [check for check in audit.checks(home) if check.state == FAIL]
    add(OK if not failed else FAIL, 'Security rules: ' + ('all pass' if not failed else f'{len(failed)} fail'),
        'wraithd audit, for each rule and its fix')

    # --- the web UI -------------------------------------------------------------------------------------------
    from wraithd.daemon.app import ui_dir

    ui = ui_dir()
    add(OK if ui else FAIL, f'Web UI: {ui}' if ui else 'Web UI: not in this install (the daemon shows a placeholder)',
        'install a release build (scripts/release.sh builds one), or set WRAITHD_UI_DIR to a built frontend/dist')

    # --- the service and the daemon ---------------------------------------------------------------------------
    try:
        service = service or service_module.service_for(home)
        state = service.status()
        if not state.installed:
            add(FAIL, 'Background service: not installed', 'wraithd daemon install')
        else:
            add(OK, f'Background service: installed ({service.path})')
            add(OK if state.running else FAIL,
                'Background service: ' + (f'running (pid {state.pid})' if state.pid else 'running' if state.running
                                          else 'not running'),
                'wraithd daemon start (and wraithd daemon logs if it stops again)')
        kind = 'launchd' if isinstance(service, service_module.Launchd) else 'systemd'
        old = rename.old_service_file(service.user_home, kind)
        if old.is_file():
            add(FAIL, f'The old Local Agent service is still installed ({old})', 'wraithd daemon install (removes it)')
    except ServiceError as e:
        add(INFO, str(e))

    port = config.daemon_port
    answer = service_module.health(port, transport)
    if answer:
        same = answer.get('version') == __version__
        add(OK if same else FAIL, f'Daemon answers on port {port} (version {answer.get("version")})',
            'it is still running the old version: wraithd daemon restart')
    else:
        add(FAIL, f'Daemon: nothing answers on port {port}', 'wraithd daemon start (or run wraithd daemon)')
        free = port_free(port)
        add(OK if free else FAIL, f'Port {port}: ' + ('free' if free else 'in use by another program'),
            f'stop that program, or set another port in {home.config_path} ([daemon] port)')
    # New checks: each worker adds `found += its_checks(home, config)` (a list of Check) inside its block only
    # >>> security
    found += subscription_checks(home, login_check)  # subscription mode
    # <<< security
    # >>> efficiency
    from wraithd.providers import doctor_checks

    model_checks = doctor_checks(home, config, transport)
    if engine_name(home) == CLAUDE_ENGINE:  # Claude Code thinks; [model] is only what a run falls back to
        model_checks = [fallback_label(check) for check in model_checks]
    found += model_checks
    # <<< efficiency
    # >>> capabilities
    # <<< capabilities
    # >>> reach
    # <<< reach
    # >>> evals
    # <<< evals
    # >>> sandbox
    from wraithd.sandbox.audit import doctor_checks as sandbox_checks

    found += sandbox_checks(home, config)
    # <<< sandbox
    return found


def fallback_label(check: Check) -> Check:
    """'Model gpt-4o (strong): ...' as 'Fallback model gpt-4o (strong): ...' (and 'Models:' likewise)."""
    if not check.text.startswith('Model'):
        return check
    return Check(check.state, 'Fallback m' + check.text[1:], check.fix)


def render(found: list[Check]) -> str:
    lines = []
    for check in found:
        lines.append(f'  {check.state:<5} {check.text}')
        if check.state in (FAIL, WARN) and check.fix:
            lines.append(f'        fix: {check.fix}')
    failed = sum(check.state == FAIL for check in found)
    lines.append('\nAll good.' if not failed else f'\n{failed} problem{"s" if failed > 1 else ""} found.')
    return '\n'.join(lines)


def main(home: Home, **kwargs) -> int:
    found = checks(home, **kwargs)
    print(render(found))
    return 1 if any(check.state == FAIL for check in found) else 0
