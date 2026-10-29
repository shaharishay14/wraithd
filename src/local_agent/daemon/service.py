"""
The daemon as a background service: a launchd agent on macOS, a systemd user unit on Linux.

Both run `local-agent daemon` (as this Python's `-m local_agent daemon`, so it survives `update` and needs no PATH
lookup), start it at login, restart it if it crashes, and append its output to ~/.local-agent/logs/daemon.log.
Nothing runs as root and nothing is installed system-wide.

The log is rotated by the daemon itself (rotate_log, at start and every minute): past 5 MB it's copied to
daemon.log.1 (older ones move to .2 and .3, the oldest dropped) and emptied in place. In place, because the service
manager holds the file open and appends to it; a few lines written during the copy can be lost.

Every launchctl/systemctl call goes through `runner` (subprocess.run by default), so tests pass a fake and never
touch the real service manager.
"""

import asyncio
import os
import plistlib
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from local_agent.config import Home

LABEL = 'com.local-agent.daemon'  # launchd
UNIT = 'local-agent.service'  # systemd
LOG_FILE = 'daemon.log'
LOG_MAX_BYTES = 5_000_000  # rotated past this
LOG_KEEP = 3  # daemon.log.1 (newest) to .3
LOG_ROTATE_EVERY_SECONDS = 60

Runner = Callable[[list[str]], subprocess.CompletedProcess]


def run_command(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


class ServiceError(Exception):
    """The service manager refused, or this system has none we support. The message says what to do."""


@dataclass(frozen=True)
class Status:
    installed: bool
    running: bool
    pid: int | None = None


def log_path(home: Home) -> Path:
    return home.root / 'logs' / LOG_FILE


def rotated_log_paths(home: Home, keep: int = LOG_KEEP) -> list[Path]:
    """daemon.log.1 (the newest) to .<keep>."""
    path = log_path(home)
    return [path.with_name(f'{LOG_FILE}.{number}') for number in range(1, keep + 1)]


def rotate_log(home: Home, max_bytes: int | None = None, keep: int = LOG_KEEP) -> bool:
    """Past max_bytes (LOG_MAX_BYTES), the log is copied to .1 (the older ones shift, the oldest goes) and emptied.
    True if it was."""
    max_bytes = LOG_MAX_BYTES if max_bytes is None else max_bytes
    path = log_path(home)
    try:
        if path.stat().st_size < max_bytes:
            return False
    except FileNotFoundError:
        return False
    older = rotated_log_paths(home, keep)
    for source, target in reversed(list(zip(older, older[1:], strict=False))):
        if source.exists():
            os.replace(source, target)
    # Copy, then empty in place: launchd and systemd keep the file open in append mode, so it must stay the same file
    descriptor = os.open(older[0], os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with open(path, 'rb') as current, os.fdopen(descriptor, 'wb') as copy:
        shutil.copyfileobj(current, copy)
    os.truncate(path, 0)
    return True


async def rotate_log_job(rt) -> None:
    """The periodic job (daemon/jobs.py)."""
    await asyncio.to_thread(rotate_log, rt.home)


def daemon_command() -> list[str]:
    return [sys.executable, '-m', 'local_agent', 'daemon']


def health(port: int, transport: httpx.BaseTransport | None = None) -> dict | None:
    """The daemon's /api/health answer, or None if nothing (or something else) answers on the port."""
    try:
        with httpx.Client(transport=transport, timeout=2) as client:
            data = client.get(f'http://127.0.0.1:{port}/api/health').json()
    except (httpx.HTTPError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get('ok') is True else None


class Service:
    """What both service managers share: where the logs go and what runs."""

    name = ''

    def __init__(self, home: Home, runner: Runner = run_command, user_home: Path | None = None,
                 program: list[str] | None = None, environment: dict[str, str] | None = None):
        self.home, self.runner = home, runner
        self.user_home = Path(user_home) if user_home else Path.home()
        self.program = program or daemon_command()
        # launchd and systemd start services with a bare PATH: keep the owner's, so MCP servers (npx, uvx) are found
        # (and unbuffered output, so the log shows lines as they happen)
        self.environment = environment if environment is not None else {
            'LOCAL_AGENT_HOME': str(home.root), 'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
            'PYTHONUNBUFFERED': '1'}

    @property
    def log_path(self) -> Path:
        return log_path(self.home)

    @property
    def path(self) -> Path:
        raise NotImplementedError

    def is_installed(self) -> bool:
        return self.path.is_file()

    def _run(self, *argv: str, check: bool = True) -> subprocess.CompletedProcess:
        result = self.runner(list(argv))
        if check and result.returncode != 0:
            problem = (result.stderr or result.stdout or '').strip() or f'exit code {result.returncode}'
            raise ServiceError(f'{argv[0]} {argv[1]} failed: {problem}')
        return result

    def _write(self, text: str | bytes) -> None:
        self.home.ensure()
        self.log_path.parent.mkdir(mode=0o700, exist_ok=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            self.path.write_bytes(text)
        else:
            self.path.write_text(text)

    def _require_installed(self) -> None:
        if not self.is_installed():
            raise ServiceError('The background service is not installed: local-agent daemon install')


class Launchd(Service):
    """A launchd agent in ~/Library/LaunchAgents, in the owner's GUI session (gui/<uid>)."""

    name = 'launchd agent'

    @property
    def path(self) -> Path:
        return self.user_home / 'Library' / 'LaunchAgents' / f'{LABEL}.plist'

    @property
    def domain(self) -> str:
        return f'gui/{os.getuid()}'

    @property
    def target(self) -> str:
        return f'{self.domain}/{LABEL}'

    def definition(self) -> bytes:
        return plistlib.dumps({
            'Label': LABEL,
            'ProgramArguments': self.program,
            'EnvironmentVariables': self.environment,
            'RunAtLoad': True,  # at login, and right away when installed
            'KeepAlive': {'SuccessfulExit': False},  # restart after a crash, not after a clean stop
            'ThrottleInterval': 10,  # a daemon that can't start (bad config) retries every 10 s, not in a tight loop
            'ProcessType': 'Background',
            'StandardOutPath': str(self.log_path),
            'StandardErrorPath': str(self.log_path),
        })

    def _loaded(self) -> subprocess.CompletedProcess | None:
        result = self._run('launchctl', 'print', self.target, check=False)
        return result if result.returncode == 0 else None

    def install(self) -> None:
        self._write(self.definition())
        if self._loaded():  # reinstalling: load the new definition
            self._run('launchctl', 'bootout', self.target)
        self._run('launchctl', 'bootstrap', self.domain, str(self.path))

    def uninstall(self) -> None:
        if self._loaded():
            self._run('launchctl', 'bootout', self.target)
        self.path.unlink(missing_ok=True)

    def start(self) -> None:
        self._require_installed()
        if not self._loaded():
            self._run('launchctl', 'bootstrap', self.domain, str(self.path))
        elif not self.status().running:
            self._run('launchctl', 'kickstart', self.target)

    def stop(self) -> None:
        """Unloads it, so KeepAlive doesn't bring it back. It loads again at the next login."""
        if self._loaded():
            self._run('launchctl', 'bootout', self.target)

    def restart(self) -> None:
        self._require_installed()
        if self._loaded():
            self._run('launchctl', 'kickstart', '-k', self.target)
        else:
            self._run('launchctl', 'bootstrap', self.domain, str(self.path))

    def status(self) -> Status:
        loaded = self._loaded()
        if not loaded:
            return Status(installed=self.is_installed(), running=False)
        pid = re.search(r'^\s*pid = (\d+)', loaded.stdout, re.MULTILINE)
        running = re.search(r'^\s*state = running', loaded.stdout, re.MULTILINE) is not None
        return Status(installed=self.is_installed(), running=running, pid=int(pid.group(1)) if pid else None)


def _systemd_quote(value: str) -> str:
    """One word in a unit file: quoted, with backslashes, quotes and % (a specifier) escaped."""
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


class Systemd(Service):
    """A systemd user unit in ~/.config/systemd/user (it runs while the owner is logged in; see docs/install.md)."""

    name = 'systemd user unit'

    @property
    def path(self) -> Path:
        return self.user_home / '.config' / 'systemd' / 'user' / UNIT

    def definition(self) -> str:
        environment = ' '.join(_systemd_quote(f'{key}={value}') for key, value in self.environment.items())
        log = str(self.log_path).replace('%', '%%')
        return '\n'.join([
            '[Unit]',
            'Description=Local Agent: the API, web UI, scheduler and Telegram',
            'After=network-online.target',
            '',
            '[Service]',
            f'ExecStart={" ".join(_systemd_quote(word) for word in self.program)}',
            f'Environment={environment}',
            'Restart=on-failure',
            'RestartSec=10',
            f'StandardOutput=append:{log}',
            f'StandardError=append:{log}',
            '',
            '[Install]',
            'WantedBy=default.target',
            '',
        ])

    def _systemctl(self, *argv: str, check: bool = True) -> subprocess.CompletedProcess:
        return self._run('systemctl', '--user', *argv, check=check)

    def install(self) -> None:
        self._write(self.definition())
        self._systemctl('daemon-reload')
        self._systemctl('enable', UNIT)
        self._systemctl('restart', UNIT)  # restart, not start: a reinstall must pick up the new definition

    def uninstall(self) -> None:
        if self.is_installed():
            self._systemctl('disable', '--now', UNIT, check=False)
        self.path.unlink(missing_ok=True)
        self._systemctl('daemon-reload', check=False)

    def start(self) -> None:
        self._require_installed()
        self._systemctl('start', UNIT)

    def stop(self) -> None:
        if self.is_installed():
            self._systemctl('stop', UNIT)

    def restart(self) -> None:
        self._require_installed()
        self._systemctl('restart', UNIT)

    def status(self) -> Status:
        if not self.is_installed():
            return Status(installed=False, running=False)
        result = self._systemctl('show', UNIT, '--property=ActiveState,MainPID', check=False)
        values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        pid = int(values.get('MainPID') or 0)
        return Status(installed=True, running=values.get('ActiveState') == 'active', pid=pid or None)


def service_for(home: Home, platform: str = sys.platform, **kwargs) -> Service:
    if platform == 'darwin':
        return Launchd(home, **kwargs)
    if platform.startswith('linux'):
        return Systemd(home, **kwargs)
    raise ServiceError('A background service is only set up on macOS and Linux. Run `local-agent daemon` instead.')
