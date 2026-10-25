"""
The shell: off unless the owner turns it on ([shell] enabled = true), and then every single command is shown to
the owner and runs only after they say yes. It never runs unattended: the gate refuses it outright when nobody
is there to ask, and run() doesn't even offer it then.

There's no sandbox (that's a later decision, see the plan). What there is: the approval on every command, a
working folder of workspace/files/, a timeout that kills the whole process group, an output cap, and an
environment without the owner's API keys.
"""

import asyncio
import os
import signal

from local_agent.core import secrets
from local_agent.tools.base import function_tool, tool_error

RUN = 'shell_run'
OUTPUT_CHARS = 10_000  # per stream; the guard caps the result for the model again


def _environment() -> dict[str, str]:
    """The owner's environment without the keys the harness knows about: a command has no need for them."""
    return {name: value for name, value in os.environ.items() if name not in secrets.ENV_SECRETS}


def _cut(data: bytes) -> str:
    text = data.decode('utf-8', errors='replace')
    return text if len(text) <= OUTPUT_CHARS else text[:OUTPUT_CHARS] + f'\n[... {len(text) - OUTPUT_CHARS} more chars]'


def shell_tool(cwd, timeout_seconds: int, enabled: bool):
    async def run_command(command: str) -> dict:
        if not enabled:  # not offered when off; this is the second lock
            return tool_error('The shell is turned off.', 'Tell the user it can be enabled in config.toml.')
        command = (command or '').strip()
        if not command:
            return tool_error('No command given.', 'Give the exact command to run.')
        process = await asyncio.create_subprocess_shell(
            command, cwd=cwd, env=_environment(), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # its own process group, so a timeout kills what it started too
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        except TimeoutError:
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
            return tool_error(f'The command did not finish within {timeout_seconds} seconds and was stopped.',
                              'Do not run it again as is. Tell the user it timed out.')
        result = {'exit_code': process.returncode, 'stdout': _cut(stdout), 'stderr': _cut(stderr)}
        if process.returncode:
            result['error'] = f'The command exited with code {process.returncode}.'
        return result

    return function_tool(
        RUN,
        ('Run one shell command on the user\'s computer, in their workspace files folder. The user sees the exact '
         'command and must approve it every time, so say why you need it. Prefer the file tools for reading and '
         'writing files. Returns exit_code, stdout and stderr.'),
        {'properties': {'command': {'type': 'string', 'description': 'The exact command, e.g. "ls -la"'}},
         'required': ['command']},
        run_command)
