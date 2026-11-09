"""
Private file modes, whatever the umask. The state folder holds memory, the event log and the owner's files, so
what wraithd makes there is the owner's alone: folders 700, files 600. The process umask can't widen that (Ubuntu's
default 002 would make everything group-writable, and the audit then fails on wraithd's own folders).

- make_dirs: like Path.mkdir(parents=True, exist_ok=True), each folder it makes 700 (mkdir's parents ignore mode)
- write_private: a new file 600, whatever the umask
- tighten_umask: drops group and other write from the process umask, for files made by code that doesn't use
  these (a library, a subprocess); what's readable stays readable
- repair: clears group/other write on what's already there (a home made under umask 002 before this fix)
"""

import os
import stat
from pathlib import Path

PRIVATE_DIR = 0o700
PRIVATE_FILE = 0o600


def make_dirs(path: Path | str, mode: int = PRIVATE_DIR) -> None:
    """Makes path and any missing parents with mode, set explicitly so the umask can't widen it. Folders that
    already exist are left as they are."""
    path = Path(path)
    missing = []
    while not path.exists():
        missing.append(path)
        if path.parent == path:
            break
        path = path.parent
    for folder in reversed(missing):
        try:
            folder.mkdir(mode=mode)
        except FileExistsError:
            continue  # made by someone else meanwhile: theirs, so not changed
        os.chmod(folder, mode)


def write_private(path: Path | str, text: str) -> None:
    """Writes a new file (or replaces one's content) with mode 600. Never follows a symlink at the name."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), PRIVATE_FILE)
    with os.fdopen(fd, 'w', encoding='utf-8') as out:
        os.fchmod(out.fileno(), PRIVATE_FILE)
        out.write(text)


def tighten_umask() -> None:
    """Group and other never get write on what this process makes. Reads os.umask by setting it, then restores
    the owner's other bits."""
    old = os.umask(0o077)
    os.umask(old | 0o022)


def repair(root: Path | str) -> list[Path]:
    """Clears group/other write on root and everything under it that this account owns. Symlinks aren't followed
    (what they point to isn't the folder's). Returns what was changed."""
    uid = os.getuid()
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        return []
    changed = _clear_write(root, uid)
    for folder, folders, files in os.walk(root):
        for name in folders + files:
            changed += _clear_write(Path(folder) / name, uid)
    return changed


def _clear_write(path: Path, uid: int) -> list[Path]:
    try:
        info = path.lstat()
    except OSError:
        return []
    if stat.S_ISLNK(info.st_mode) or info.st_uid != uid or not info.st_mode & 0o022:
        return []
    try:
        os.chmod(path, stat.S_IMODE(info.st_mode) & ~0o022)
    except OSError:
        return []
    return [path]
