"""
What wraithd makes in its state folder is the owner's alone (folders 700, files 600), whatever the umask: Ubuntu's
default 002 made the workspace group-writable, and wraithd's own audit then failed (store/modes.py).
"""

import json
import os
import stat

import pytest

from wraithd import audit
from wraithd.config import Home
from wraithd.memory.notes import Note, Vault
from wraithd.store import SQLiteStore, modes
from wraithd.tools.files import file_tools, save_new


@pytest.fixture(autouse=True)
def ubuntu_umask():
    """umask 002 for the test, and back after: Home.ensure tightens the process umask."""
    previous = os.umask(0o002)
    yield
    os.umask(previous)


def mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def everything(root):
    for folder, folders, files in os.walk(root):
        for name in folders + files:
            yield os.path.join(folder, name)


def test_a_new_home_is_private_under_umask_002(tmp_path):
    home = Home(tmp_path / 'deep' / 'home')
    home.ensure()
    assert mode(home.root) == 0o700
    for path in everything(home.root):
        assert not mode(path) & 0o077, path
    assert audit._loose(home) == ([], [])


def test_a_home_made_group_writable_before_is_repaired(tmp_path):
    os.umask(0o002)
    root = tmp_path / 'home'
    for folder in ('workspace/memory/people', 'workspace/files', 'workspace/skills/mine'):
        (root / folder).mkdir(parents=True)
    (root / 'workspace' / 'memory' / 'people' / 'dana.md').write_text('note')
    (root / 'workspace' / 'skills' / 'mine' / 'your-way.md').write_text('- rule')
    outside = tmp_path / 'shared.txt'
    outside.write_text('not the home\'s')
    (root / 'workspace' / 'files' / 'link').symlink_to(outside)
    assert mode(root / 'workspace') & 0o022

    home = Home(root)
    home.ensure()
    assert audit._loose(home) == ([], [])
    assert mode(outside) & 0o020  # a symlink's target isn't the folder's: left alone
    assert mode(root / 'workspace' / 'memory' / 'people' / 'dana.md') == 0o644  # readable stays readable


def test_ensure_tightens_the_process_umask(tmp_path):
    Home(tmp_path / 'home').ensure()
    (tmp_path / 'later.txt').write_text('made by a library')
    assert not mode(tmp_path / 'later.txt') & 0o022


async def test_workspace_files_are_private(tmp_path):
    root = tmp_path / 'files'
    modes.make_dirs(root)
    tools = {tool.name: tool for tool in file_tools(root)}
    for arguments in ({'path': 'a/b/new.md', 'content': 'x'}, {'path': 'c/log.md', 'content': 'x', 'append': True}):
        result = json.loads(await tools['files_write'].on_invoke_tool(None, json.dumps(arguments)))
        assert 'error' not in result
    saved = save_new(root, 'uploads/today', 'photo.jpg', b'jpeg')
    for path in ('a', 'a/b', 'c', 'uploads', 'uploads/today'):
        assert mode(root / path) == 0o700, path
    for path in ('a/b/new.md', 'c/log.md', saved):
        assert mode(root / path) == 0o600, path


def test_memory_notes_and_the_store_are_private(tmp_path):
    Vault(tmp_path / 'memory').save(Note(path='people/dana.md', title='Dana', body='likes tea'))
    assert mode(tmp_path / 'memory') == mode(tmp_path / 'memory' / 'people') == 0o700
    assert mode(tmp_path / 'memory' / 'people' / 'dana.md') == 0o600
    SQLiteStore(tmp_path / 'state' / 'state.db').close()
    assert mode(tmp_path / 'state') == 0o700 and mode(tmp_path / 'state' / 'state.db') == 0o600


def test_make_dirs_leaves_existing_folders_alone(tmp_path):
    (tmp_path / 'there').mkdir(mode=0o755)
    os.chmod(tmp_path / 'there', 0o755)
    modes.make_dirs(tmp_path / 'there' / 'new')
    assert mode(tmp_path / 'there') == 0o755 and mode(tmp_path / 'there' / 'new') == 0o700


def test_write_private_does_not_follow_a_symlink(tmp_path):
    (tmp_path / 'target').write_text('keep')
    (tmp_path / 'link').symlink_to(tmp_path / 'target')
    with pytest.raises(OSError):
        modes.write_private(tmp_path / 'link', 'overwrite')
    assert (tmp_path / 'target').read_text() == 'keep'
