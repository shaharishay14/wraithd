"""
Spill: a tool result too big for the context is saved whole to a file, and the model gets what it got before (the
first MAX_TOOL_RESULT_CHARS) plus the size and where the rest is. It never sees less than without spilling, and
can page through the rest with files_read.

    workspace/files/.spill/<run id>/<n>-<tool>.txt

- The file is redacted before it's written (core/secrets.py), like everything else the harness stores.
- Taint survives: the gate tracks the full text, wraps the preview as untrusted when the result was, and reading
  the file back with files_read is third-party content (it taints the run, as any file does).
- files_read itself is never spilled: it already returns one part at a time.
- Old run folders are deleted after [spill] keep_days by a daemon job (cleanup).
"""

import logging
import os
import shutil
import time
from pathlib import Path

from local_agent.core import events as ev
from local_agent.core import render, secrets
from local_agent.tools.files import safe_name

logger = logging.getLogger(__name__)

FOLDER = '.spill'  # inside workspace/files/, so files_read can page through a spilled result
NOT_SPILLED = {'files_read'}  # tools that already return one part at a time
CLEANUP_EVERY_SECONDS = 24 * 3600


def spill_folder(home) -> Path:
    return home.files_dir / FOLDER


def save(home, run_id: str, number: int, tool: str, text: str) -> Path:
    """Writes one redacted result, private to the owner. Returns its path."""
    folder = spill_folder(home) / safe_name(run_id, 'run')
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / f'{number}-{safe_name(tool, "tool")}.txt'
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as file:
        file.write(secrets.redact_text(text))
    return path


def note(path: str, shown: int, total: int) -> str:
    return (f'\n\n[SPILLED: showing the first {shown:,} of {total:,} characters. The full output is saved at '
            f'{path!r}; read the rest with files_read(path={path!r}, offset={shown}).]')


def spill_hook(rt, log):
    """The tool-result hook (core/tool_results.py): spills results over [spill] over_chars."""
    over = rt.config.section('spill')['over_chars']
    count = 0

    def spill(tool: str, text: str, untrusted: bool) -> str:
        nonlocal count
        if log is None or over <= 0 or len(text) <= over or tool in NOT_SPILLED:
            return text
        count += 1
        path = save(rt.home, log.run_id, count, tool, text)
        shown_path = path.relative_to(rt.home.files_dir).as_posix()
        preview = text[:render.MAX_TOOL_RESULT_CHARS]
        log.append(ev.SPILLED, tool=tool, chars=len(text), path=shown_path, untrusted=untrusted)
        return render.Bounded(preview + note(shown_path, len(preview), len(text)))

    return spill


def cleanup(home, keep_days: int, now: float | None = None) -> list[str]:
    """Deletes run folders not changed for keep_days (0 or less: keeps them). Returns the folders deleted."""
    root = spill_folder(home)
    if keep_days <= 0 or not root.is_dir():
        return []
    cutoff = (now or time.time()) - keep_days * 24 * 3600
    deleted = []
    for folder in sorted(root.iterdir()):
        if folder.is_symlink() or not folder.is_dir() or folder.stat().st_mtime >= cutoff:
            continue
        shutil.rmtree(folder, ignore_errors=True)
        deleted.append(folder.name)
    return deleted


async def cleanup_job(rt) -> None:
    """The daemon's daily job: old spill folders go, and the deletion is an event."""
    keep_days = rt.config.section('spill')['keep_days']
    deleted = cleanup(rt.home, keep_days)
    if deleted:
        ev.record_event(rt.store, ev.SPILL_CLEANED, folders=len(deleted), keep_days=keep_days)
