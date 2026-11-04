"""
wraithd leaves no conversation transcripts or project files behind in ~/.claude. Warm sessions keep the conversation
in the running Claude Code process, and a new session is rebuilt from wraithd's own transcript, so nothing needs
Claude Code's saved sessions: claude_options() turns them off (--no-session-persistence).

Claude Code's own bookkeeping is expected and allowed: ~/.claude.json, its plugin cache (plugins/), and temporary
files or timestamp changes in its sessions/ and backups/ folders. The owner's smoke test on the real login saw
exactly those change, and nothing under ~/.claude/projects.

With the fake SDK this proves that wraithd itself leaves no transcript. The real Claude Code binary is not run here;
that it honors --no-session-persistence was checked in the Phase 0 spike (scripts/spike_claude.py) and the smoke test.
"""

from pathlib import Path

from wraithd.config import Home, set_values
from wraithd.engines import claude_code
from wraithd.engines.claude_options import claude_options
from wraithd.run import Runtime, run

TRANSCRIPT_SUFFIXES = ('.jsonl',)  # how Claude Code saves a conversation


def files_under(folder: Path) -> set[Path]:
    return {path for path in folder.rglob('*') if path.is_file()} if folder.exists() else set()


def left_behind(claude_dir: Path, before: set[Path]) -> list[Path]:
    """New files that break the rule: anything under projects/, or a transcript anywhere. Bookkeeping is fine."""
    return sorted(path for path in files_under(claude_dir) - before
                  if path.relative_to(claude_dir).parts[0] == 'projects' or path.suffix in TRANSCRIPT_SUFFIXES)


def test_bookkeeping_is_allowed_and_transcripts_are_not(tmp_path):
    claude_dir = tmp_path / '.claude'
    (claude_dir / 'projects').mkdir(parents=True)
    before = files_under(claude_dir)
    for allowed in ('plugins/store/cc-plugin-agents-md_builtin-1.json', 'sessions/tmp-1', 'backups/x.json'):
        (claude_dir / allowed).parent.mkdir(parents=True, exist_ok=True)
        (claude_dir / allowed).write_text('{}')
    (tmp_path / '.claude.json').write_text('{}')  # beside ~/.claude, not in it
    assert left_behind(claude_dir, before) == []

    (claude_dir / 'projects' / '-Users-me').mkdir()
    (claude_dir / 'projects' / '-Users-me' / 'abc.jsonl').write_text('{}')
    (claude_dir / 'sessions' / 'abc.jsonl').write_text('{}')
    assert [p.name for p in left_behind(claude_dir, before)] == ['abc.jsonl', 'abc.jsonl']


async def test_runs_leave_no_transcripts_under_dot_claude(tmp_path, monkeypatch, claude_sdk):
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
    assert left_behind(claude_dir, before) == []
    assert files_under(claude_dir / 'projects') == set()


def test_the_options_turn_session_saving_off(claude_sdk):
    claude_sdk()
    assert claude_options('x', None, [], 'haiku').extra_args == {'no-session-persistence': None}
    assert claude_options('x', {'type': 'sdk'}, ['mcp__wraithd__x'], 'sonnet', stream=True).extra_args == {
        'no-session-persistence': None}
