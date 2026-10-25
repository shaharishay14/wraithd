"""
`local-agent update`: upgrade with whichever tool installed it, then restart the background service if there is one.

The installer is found from this Python's environment: uv tool environments have a uv-receipt.toml, pipx
environments a pipx_metadata.json. Anything else (a git checkout, a plain pip install) is left to the owner, since
guessing could upgrade the wrong environment.
"""

import shutil
import subprocess
import sys
from pathlib import Path

from local_agent.config import Home
from local_agent.daemon import service as service_module
from local_agent.daemon.service import ServiceError, run_command

PACKAGE = 'local-agent'
COMMANDS = {'uv': ['uv', 'tool', 'upgrade', PACKAGE], 'pipx': ['pipx', 'upgrade', PACKAGE]}


def installer(prefix: Path | None = None) -> str | None:
    """'uv', 'pipx', or None when this environment wasn't made by either."""
    prefix = Path(prefix or sys.prefix)
    if (prefix / 'uv-receipt.toml').is_file():
        return 'uv'
    if (prefix / 'pipx_metadata.json').is_file():
        return 'pipx'
    return None


def update(home: Home, runner=run_command, prefix: Path | None = None, which=shutil.which, service=None,
           say=print) -> int:
    tool = installer(prefix)
    if tool is None:
        say(f'This copy of Local Agent ({sys.prefix}) wasn\'t installed with uv or pipx, so update it the way you '
            f'installed it (for a checkout: git pull, then uv sync).')
        return 1
    if not which(tool):
        say(f'Installed with {tool}, but `{tool}` isn\'t on the PATH. Run: {" ".join(COMMANDS[tool])}')
        return 1
    say(f'Updating with {tool}: {" ".join(COMMANDS[tool])}')
    result: subprocess.CompletedProcess = runner(COMMANDS[tool])
    for text in (result.stdout, result.stderr):
        if text and text.strip():
            say(text.strip())
    if result.returncode != 0:
        say('The update failed; nothing was restarted.')
        return 1
    try:
        service = service or service_module.service_for(home)
        if service.is_installed():
            service.restart()
            say('Restarted the background service on the new version.')
    except ServiceError as e:
        if service is not None and service.is_installed():
            say(f'Updated, but the restart failed: {e}')
            return 1
    return 0
