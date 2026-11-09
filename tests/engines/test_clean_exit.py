"""
`wraithd doctor` (and any one-shot, like `wraithd heartbeat now`) ends without "RuntimeError: Event loop is closed"
from BaseSubprocessTransport.__del__: the Claude Code process is closed before the event loop ends. Runs the real
claude_agent_sdk against a fake `claude` program (fake_claude_cli.py): no model is called, nothing in ~/.claude is
touched.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip('claude_agent_sdk')

FAKE_CLI = Path(__file__).with_name('fake_claude_cli.py')
REPO = Path(__file__).parents[2]

# Runs in a fresh interpreter, so the event loop really closes and the interpreter really exits
SCRIPT = textwrap.dedent('''
    import asyncio, gc, sys
    from asyncio.base_subprocess import BaseSubprocessTransport
    from claude_agent_sdk._internal.transport import subprocess_cli
    subprocess_cli.SubprocessCLITransport._find_bundled_cli = lambda self: sys.argv[1]

    def open_transports():
        gc.collect()
        return [o for o in gc.get_objects() if isinstance(o, BaseSubprocessTransport) and not o.is_closing()]

    async def doctor():
        from wraithd import doctor
        from wraithd.config import Home
        found = await doctor.claude_login_check(Home.locate())
        return found, open_transports()

    async def one_shot():
        from wraithd.engines import claude_code
        found = await claude_code.run_once('Be brief.', 'hi', [], 'haiku')
        return found[0], open_transports()

    print(asyncio.run({'doctor': doctor, 'one_shot': one_shot}[sys.argv[2]]()))
''')


@pytest.mark.parametrize('mode', ['', 'grandchild'])
@pytest.mark.parametrize('which', ['doctor', 'one_shot'])
def test_no_event_loop_is_closed_on_exit(tmp_path, which, mode):
    os.chmod(FAKE_CLI, 0o755)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('ANTHROPIC_', 'CLAUDE_CODE_', 'WRAITHD_'))}
    env.update(HOME=str(tmp_path), WRAITHD_HOME=str(tmp_path / '.wraithd'), FAKE_CLAUDE_MODE=mode,
               PYTHONWARNINGS='default')
    done = subprocess.run([sys.executable, '-c', SCRIPT, str(FAKE_CLI), which], env=env, cwd=REPO,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    expected = "((True, 'Claude subscription login in use'), [])" if which == 'doctor' else "('OK', [])"
    assert done.stdout.strip() == expected  # answered, and no Claude Code process left open when the loop ends
    assert 'Event loop is closed' not in done.stderr and 'unclosed' not in done.stderr.lower()
