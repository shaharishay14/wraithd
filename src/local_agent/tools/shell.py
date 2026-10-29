"""
The shell: off unless the owner turns it on ([shell] enabled = true), and then every single command is shown to
the owner and runs only after they say yes. It never runs unattended: the gate refuses it outright when nobody
is there to ask, and run() doesn't even offer it then.

Without a sandbox ([sandbox] backend = "none", the default) what there is: the approval on every command, a working
folder of workspace/files/, a timeout that kills the whole process group, an output cap, and an environment without
the owner's API keys. With backend = "docker" the same command runs in a container that sees only workspace/files/
and has no network (local_agent.sandbox); a broken sandbox setting refuses, it never falls back to running as the
owner.
"""

import asyncio
import os
import signal

from local_agent import sandbox as sandbox_module
from local_agent.core import events as ev
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


TIMED_OUT = 'Do not run it again as is. Tell the user it timed out.'


async def _in_sandbox(sandbox, command: str, timeout_seconds: int) -> dict:
    if isinstance(sandbox, sandbox_module.Broken):
        sandbox_module.record(ev.SANDBOX_REFUSED, problems=list(sandbox.problems))
        return tool_error(f'The sandbox is set up wrong, so nothing runs: {"; ".join(sandbox.problems)}.',
                          'Tell the user to run local-agent audit and fix [sandbox] in config.toml.')
    try:
        result, name = await sandbox.run(command, timeout_seconds, _environment())
    except FileNotFoundError:
        return tool_error('Docker is not installed, so the sandboxed shell cannot run.',
                          'Tell the user to install Docker, or set [sandbox] backend = "none".')
    sandbox_module.record(ev.SANDBOX_RUN, backend=sandbox.name, image=sandbox.image, network=sandbox.network,
                          mounts=[sandbox.mount_source], container=name, exit_code=result.exit_code,
                          timed_out=result.timed_out)
    if result.timed_out:
        return tool_error(f'The command did not finish within {timeout_seconds} seconds and was stopped.', TIMED_OUT)
    return _result(result.exit_code, result.stdout, result.stderr)


def _result(exit_code: int, stdout: bytes, stderr: bytes) -> dict:
    result = {'exit_code': exit_code, 'stdout': _cut(stdout), 'stderr': _cut(stderr)}
    if exit_code:
        result['error'] = f'The command exited with code {exit_code}.'
    return result


def shell_tool(cwd, timeout_seconds: int, enabled: bool, sandbox=None):
    """sandbox: None runs as the owner; a sandbox backend (or sandbox.Broken, which refuses) runs it there."""
    async def run_command(command: str) -> dict:
        if not enabled:  # not offered when off; this is the second lock
            return tool_error('The shell is turned off.', 'Tell the user it can be enabled in config.toml.')
        command = (command or '').strip()
        if not command:
            return tool_error('No command given.', 'Give the exact command to run.')
        if sandbox is not None:
            return await _in_sandbox(sandbox, command, timeout_seconds)
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
                              TIMED_OUT)
        return _result(process.returncode, stdout, stderr)

    where = ('in a sandbox that sees only their workspace files folder (at /workspace)' if sandbox is not None else
             'on the user\'s computer, in their workspace files folder')
    return function_tool(
        RUN,
        (f'Run one shell command {where}. The user sees the exact '
         'command and must approve it every time, so say why you need it. Prefer the file tools for reading and '
         'writing files. Returns exit_code, stdout and stderr.'),
        {'properties': {'command': {'type': 'string', 'description': 'The exact command, e.g. "ls -la"'}},
         'required': ['command']},
        run_command)
