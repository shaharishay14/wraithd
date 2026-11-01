"""
The move from the old name, Local Agent, to wraithd: one release of grace, then this file can go.

Everything about the old name is here, so deleting it later is one file and the few calls into it (config.py,
mcp/config.py, core/secrets.py, daemon/service.py, doctor.py, and their tests):

- state: ~/.local-agent is moved (renamed, never merged) to ~/.wraithd (or $WRAITHD_HOME) on first use;
  $LOCAL_AGENT_HOME still works while $WRAITHD_HOME isn't set
- keys: read from the keychain under 'wraithd' first, then the old 'local-agent' service; written to 'wraithd' only
- MCP values in the environment: LOCAL_AGENT_MCP_* still read (and kept out of the shell) after WRAITHD_MCP_*
- the background service: `wraithd daemon install` stops and removes the old one; doctor names a leftover
"""

import os
import sys
from pathlib import Path

import keyring
import keyring.errors

OLD_HOME_ENV = 'LOCAL_AGENT_HOME'
OLD_FOLDER = '.local-agent'
OLD_APP = 'local-agent'  # the old keychain service
OLD_MCP_ENV_PREFIX = 'LOCAL_AGENT_MCP_'
OLD_LABEL = 'com.local-agent.daemon'  # launchd
OLD_UNIT = 'local-agent.service'  # systemd

_said: set[str] = set()


def _say(line: str) -> None:
    """One line on stderr, once per process (stdout may be a command's output)."""
    if line not in _said:
        _said.add(line)
        print(line, file=sys.stderr)


# --- the state folder ---------------------------------------------------------------------------------------

def old_home() -> Path:
    return Path.home() / OLD_FOLDER


def both_homes(root: Path) -> bool:
    """True when the old folder was left beside the new one (they are never merged): doctor reports it."""
    return root.is_dir() and old_home().is_dir() and old_home().resolve() != root.resolve()


def locate(new_env: str | None, default: Path) -> Path:
    """
    The state folder: $WRAITHD_HOME (new_env), else $LOCAL_AGENT_HOME, else `default` (~/.wraithd). The first time,
    an old ~/.local-agent is moved to the new place, and one line says so. Both there: neither is touched.
    """
    old_env = os.environ.get(OLD_HOME_ENV)
    if not new_env and old_env:
        return Path(old_env).expanduser()  # the old variable, for one release
    target = Path(new_env or default).expanduser()
    old = old_home()
    if old_env and Path(old_env).expanduser().resolve() != old.resolve():
        return target  # the owner's old state is somewhere else: ~/.local-agent isn't theirs to move
    if not old.is_dir() or old.resolve() == target.resolve():
        return target
    if target.exists():
        _say(f'wraithd: both {old} and {target} exist; using {target} and leaving {old} as it is.')
        return target
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(old, target)
    except OSError as e:
        _say(f'wraithd: could not move {old} to {target} ({e}); using {old} for now. Move it yourself to finish.')
        return old
    _say(f'wraithd: moved your state from {old} to {target} (Local Agent is now wraithd).')
    return target


# --- keys --------------------------------------------------------------------------------------------------

def old_keychain_get(account: str) -> str | None:
    """A value saved under the old keychain service, or None."""
    try:
        return keyring.get_password(OLD_APP, account)
    except keyring.errors.NoKeyringError:
        return None


def old_mcp_env(name: str) -> str | None:
    """WRAITHD_MCP_X_KEY's value under its old name, LOCAL_AGENT_MCP_X_KEY."""
    from wraithd.core.secrets import MCP_ENV_PREFIX

    if not name.startswith(MCP_ENV_PREFIX):
        return None
    return os.environ.get(OLD_MCP_ENV_PREFIX + name[len(MCP_ENV_PREFIX):]) or None


# --- the background service --------------------------------------------------------------------------------

def old_service_file(user_home: Path, kind: str) -> Path:
    """kind: 'launchd' or 'systemd'."""
    if kind == 'launchd':
        return user_home / 'Library' / 'LaunchAgents' / f'{OLD_LABEL}.plist'
    return user_home / '.config' / 'systemd' / 'user' / OLD_UNIT


def remove_old_service(user_home: Path, kind: str, runner) -> Path | None:
    """Stops and removes the old service, if it's there. The file removed, or None. Failures to stop are ignored:
    removing the file is what keeps it from coming back."""
    path = old_service_file(user_home, kind)
    if not path.is_file():
        return None
    if kind == 'launchd':
        runner(['launchctl', 'bootout', f'gui/{os.getuid()}/{OLD_LABEL}'])
    else:
        runner(['systemctl', '--user', 'disable', '--now', OLD_UNIT])
    path.unlink(missing_ok=True)
    if kind == 'systemd':
        runner(['systemctl', '--user', 'daemon-reload'])
    _say(f'wraithd: removed the old Local Agent service ({path}).')
    return path
