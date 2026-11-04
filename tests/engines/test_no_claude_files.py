"""
wraithd leaves nothing in ~/.claude. Warm sessions keep the conversation in the running Claude Code process, and a
new session is rebuilt from wraithd's own transcript, so nothing needs Claude Code's saved sessions:
claude_options() turns them off (--no-session-persistence).

With the fake SDK this proves that wraithd itself writes nothing under ~/.claude. The real Claude Code binary is
not run here; that it honors --no-session-persistence was checked in the Phase 0 spike (scripts/spike_claude.py).
"""

from pathlib import Path

from wraithd.config import Home, set_values
from wraithd.engines import claude_code
from wraithd.engines.claude_options import claude_options
from wraithd.run import Runtime, run


def files_under(folder: Path) -> set[Path]:
    return set(folder.rglob('*')) if folder.exists() else set()


async def test_runs_leave_no_files_under_dot_claude(tmp_path, monkeypatch, claude_sdk):
    user = tmp_path / 'user'
    claude_dir = user / '.claude'
    (claude_dir / 'projects').mkdir(parents=True)
    (claude_dir / 'settings.json').write_text('{}')
    monkeypatch.setenv('HOME', str(user))  # never the real home (the root conftest also points HOME at a temp folder)
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(claude_dir))
    before = files_under(claude_dir)

    fake = claude_sdk()  # every query and session message answers with init + result
    home = Home.locate()
    set_values(home, 'engine', {'name': 'claude-code'})
    rt = Runtime.open(home)
    try:
        await claude_code.run_once('prompt', 'one-shot', [], 'haiku')
        await run(rt, 'first', conversation='c1')
        await run(rt, 'second', conversation='c1')
    finally:
        await rt.close_sessions()
        rt.close()

    assert len(fake.calls) == 3 and len(fake.ClaudeSDKClient.instances) == 1  # one one-shot, one warm session
    for used in fake.calls:
        assert used['options'].extra_args == {'no-session-persistence': None}
    assert files_under(claude_dir) == before


def test_the_options_turn_session_saving_off(claude_sdk):
    claude_sdk()
    assert claude_options('x', None, [], 'haiku').extra_args == {'no-session-persistence': None}
    assert claude_options('x', {'type': 'sdk'}, ['mcp__wraithd__x'], 'sonnet', stream=True).extra_args == {
        'no-session-persistence': None}
