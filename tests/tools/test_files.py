"""
Workspace files: list, read and write inside workspace/files/, and nothing outside it, however the path is
written (`..`, absolute, `~`, or a symlink inside the folder that points out). Through the real run().
"""

import json
import os

import pytest

from local_agent.core import events as ev
from local_agent.tools import files as files_module
from local_agent.tools.files import OutsideWorkspace, file_tools, resolve_inside, safe_name, save_new
from tests.scripted import ScriptedModel, call, of_type, run_once, say


@pytest.fixture
def files(home):
    root = home.files_dir
    (root / 'notes').mkdir()
    (root / 'notes' / 'todo.md').write_text('- buy milk\n')
    return root


@pytest.fixture
def outside(home, tmp_path):
    secret = tmp_path / 'outside' / 'secret.txt'
    secret.parent.mkdir()
    secret.write_text('the owner\'s ssh key')
    return secret


def by_name(root):
    return {tool.name: tool for tool in file_tools(root)}


async def call_tool(tool, **arguments):
    return json.loads(await tool.on_invoke_tool(None, json.dumps(arguments)))


# --- confinement -------------------------------------------------------------------------------------------

@pytest.mark.parametrize('path', ['../outside/secret.txt', 'notes/../../outside/secret.txt', '/etc/passwd',
                                  '~/.ssh/id_rsa', '..'])
def test_paths_that_leave_the_folder_are_refused(files, path):
    with pytest.raises(OutsideWorkspace):
        resolve_inside(files, path)


def test_a_symlink_pointing_out_is_refused_but_one_inside_works(files, outside):
    (files / 'escape.txt').symlink_to(outside)
    (files / 'escape-dir').symlink_to(outside.parent, target_is_directory=True)
    (files / 'todo-link.md').symlink_to(files / 'notes' / 'todo.md')
    for path in ['escape.txt', 'escape-dir/secret.txt', 'escape-dir']:
        with pytest.raises(OutsideWorkspace):
            resolve_inside(files, path)
    assert resolve_inside(files, 'todo-link.md') == (files / 'notes' / 'todo.md').resolve()


async def test_no_tool_reads_writes_or_lists_outside(files, outside):
    tools = by_name(files)
    (files / 'escape.txt').symlink_to(outside)
    (files / 'escape-dir').symlink_to(outside.parent, target_is_directory=True)
    for path in ['../outside/secret.txt', 'escape.txt', str(outside)]:
        result = await call_tool(tools['files_read'], path=path)
        assert 'error' in result and 'ssh key' not in json.dumps(result)
    assert 'error' in await call_tool(tools['files_write'], path='escape.txt', content='overwritten')
    assert 'error' in await call_tool(tools['files_write'], path='escape-dir/new.txt', content='x')
    assert 'error' in await call_tool(tools['files_list'], path='escape-dir')
    assert outside.read_text() == 'the owner\'s ssh key' and not (outside.parent / 'new.txt').exists()
    # and a symlink out isn't even listed
    listed = await call_tool(tools['files_list'])
    assert [entry['path'] for entry in listed['entries']] == ['notes']


# --- the tools ---------------------------------------------------------------------------------------------

# --- a symlink swapped in after the check (the race) ------------------------------------------------------

@pytest.fixture
def after_the_check(monkeypatch):
    """after_the_check(swap): runs swap() right after the next path check passes, as a racing local process would."""
    def arm(swap):
        check = files_module.resolve_inside

        def racing(root, path):
            real = check(root, path)
            monkeypatch.setattr(files_module, 'resolve_inside', check)  # only once
            swap()
            return real
        monkeypatch.setattr(files_module, 'resolve_inside', racing)
    return arm


@pytest.fixture
def decoy(outside):
    """A folder outside the workspace that looks like workspace/files/notes."""
    (outside.parent / 'todo.md').write_text('the owner\'s ssh key')
    return outside.parent


def swap_for_link(path, target):
    path.rename(path.with_name(path.name + '-moved'))
    path.symlink_to(target)


async def test_a_file_swapped_for_a_symlink_after_the_check_is_not_read(files, outside, after_the_check):
    after_the_check(lambda: swap_for_link(files / 'notes' / 'todo.md', outside))
    result = await call_tool(by_name(files)['files_read'], path='notes/todo.md')
    assert 'changed while it was being opened' in result['error'] and 'ssh key' not in json.dumps(result)


async def test_a_folder_swapped_for_a_symlink_after_the_check_is_not_read(files, decoy, after_the_check):
    after_the_check(lambda: swap_for_link(files / 'notes', decoy))
    result = await call_tool(by_name(files)['files_read'], path='notes/todo.md')
    assert 'changed' in result['error'] and 'ssh key' not in json.dumps(result)


@pytest.mark.parametrize('append', [False, True])
async def test_a_write_into_a_swapped_folder_writes_nothing_outside(files, decoy, after_the_check, append):
    after_the_check(lambda: swap_for_link(files / 'notes', decoy))
    result = await call_tool(by_name(files)['files_write'], path='notes/todo.md', content='pwned', append=append)
    assert 'changed' in result['error']
    assert (decoy / 'todo.md').read_text() == 'the owner\'s ssh key' and sorted(p.name for p in decoy.iterdir()) == [
        'secret.txt', 'todo.md']


async def test_a_file_swapped_for_a_symlink_is_replaced_not_followed(files, outside, after_the_check):
    after_the_check(lambda: swap_for_link(files / 'notes' / 'todo.md', outside))
    tools = by_name(files)
    result = await call_tool(tools['files_write'], path='notes/todo.md', content='- milk')
    assert result['written'] == 'notes/todo.md' and outside.read_text() == 'the owner\'s ssh key'
    assert not (files / 'notes' / 'todo.md').is_symlink() and (files / 'notes' / 'todo.md').read_text() == '- milk'
    after_the_check(lambda: swap_for_link(files / 'notes' / 'todo.md', outside))
    appended = await call_tool(tools['files_write'], path='notes/todo.md', content='pwned', append=True)
    assert 'changed' in appended['error'] and outside.read_text() == 'the owner\'s ssh key'


async def test_list_read_and_write_inside(files):
    tools = by_name(files)
    assert (await call_tool(tools['files_list'], path='notes'))['entries'] == [
        {'path': 'notes/todo.md', 'type': 'file', 'bytes': 11}]
    assert (await call_tool(tools['files_read'], path='notes/todo.md'))['content'] == '- buy milk\n'
    written = await call_tool(tools['files_write'], path='drafts/letter.md', content='Dear Dana')
    assert written == {'written': 'drafts/letter.md', 'bytes': 9, 'appended': False}
    await call_tool(tools['files_write'], path='drafts/letter.md', content=',\nThanks', append=True)
    assert (files / 'drafts' / 'letter.md').read_text() == 'Dear Dana,\nThanks'


async def test_long_files_are_read_in_parts_and_binary_is_refused(files):
    tools = by_name(files)
    (files / 'long.txt').write_text('x' * 5000)
    first = await call_tool(tools['files_read'], path='long.txt')
    assert first['truncated'] and first['next_offset'] == 3500 and len(first['content']) == 3500
    second = await call_tool(tools['files_read'], path='long.txt', offset=first['next_offset'])
    assert len(second['content']) == 1500 and not second['truncated']
    (files / 'photo.jpg').write_bytes(b'\xff\xd8\x00\x10JFIF')
    assert 'binary' in (await call_tool(tools['files_read'], path='photo.jpg'))['error']


# --- saving what someone sent (Telegram attachments) -------------------------------------------------------

@pytest.mark.parametrize('name,safe', [('report.pdf', 'report.pdf'), ('../../etc/passwd', 'passwd'),
                                       ('.bashrc', 'bashrc'), ('a\\b\\c.docx', 'c.docx'), ('', 'file'),
                                       ('...', 'file'), ('in*voice?.pdf', 'in_voice_.pdf'),
                                       ('x' * 300 + '.pdf', 'x' * 96 + '.pdf')])
def test_names_from_others_are_made_safe(name, safe):
    assert safe_name(name) == safe


def test_save_new_never_replaces_a_file(files):
    assert save_new(files, 'inbox', 'scan.pdf', b'one') == 'inbox/scan.pdf'
    assert save_new(files, 'inbox', 'scan.pdf', b'two') == 'inbox/scan-2.pdf'
    assert save_new(files, 'inbox', 'notes', b'three') == 'inbox/notes'
    assert save_new(files, 'inbox', 'notes', b'four') == 'inbox/notes-2'
    assert (files / 'inbox' / 'scan.pdf').read_bytes() == b'one' and (files / 'inbox' / 'scan-2.pdf').read_bytes() \
        == b'two'


def test_save_new_into_a_folder_that_points_out_saves_nothing(files, outside):
    (files / 'inbox').symlink_to(outside.parent, target_is_directory=True)
    with pytest.raises(OutsideWorkspace):
        save_new(files, 'inbox', 'evil.sh', b'#!/bin/sh')
    assert sorted(p.name for p in outside.parent.iterdir()) == ['secret.txt']


async def test_save_new_into_a_folder_swapped_after_the_check_saves_nothing(files, decoy, after_the_check):
    (files / 'inbox').mkdir()
    after_the_check(lambda: swap_for_link(files / 'inbox', decoy))
    with pytest.raises(OutsideWorkspace):
        save_new(files, 'inbox', 'evil.sh', b'#!/bin/sh')
    assert sorted(p.name for p in decoy.iterdir()) == ['secret.txt', 'todo.md']


# --- in runs -----------------------------------------------------------------------------------------------

async def test_the_agent_saves_and_reads_a_file_without_asking(make_runtime, files, events):
    model = ScriptedModel([
        [call('1', 'files_write', {'path': 'plans/trip.md', 'content': '# Lisbon\n- flights'})],
        [call('2', 'files_read', {'path': 'plans/trip.md'})],
        [say('Saved your trip plan.')],
    ])
    result, events_ = await run_once(make_runtime(model), 'save a trip plan for Lisbon', callback=events)
    assert result.answer == 'Saved your trip plan.'
    assert (files / 'plans' / 'trip.md').read_text() == '# Lisbon\n- flights'
    assert not any(event == 'approval_required' for event, _ in events.recorded)  # writes inside need no approval
    # a document's content is third-party data: wrapped, and the run counts as tainted
    assert model.tool_outputs(2)['call_2'].startswith('<untrusted_tool_output tool="files_read">')
    assert of_type(events_, ev.TOOL_RESULT)[-1].payload['untrusted'] is True


async def test_a_path_escape_in_a_run_reads_nothing(make_runtime, files, outside):
    (files / 'escape.txt').symlink_to(outside)
    model = ScriptedModel([[call('1', 'files_read', {'path': '../outside/secret.txt'})],
                           [call('2', 'files_read', {'path': 'escape.txt'})],
                           [call('3', 'files_read', {'path': os.fspath(outside)})],
                           [say('I can only read files in your workspace.')]])
    _, events = await run_once(make_runtime(model), 'read my secret')
    outputs = [model.tool_outputs(turn)[f'call_{turn}'] for turn in (1, 2, 3)]
    assert all('ssh key' not in output for output in outputs)
    assert [e.payload['outcome'] for e in of_type(events, ev.TOOL_RESULT)] == ['error'] * 3


async def test_strangers_get_no_file_tools(make_runtime):
    model = ScriptedModel([[say('hi')]])
    await run_once(make_runtime(model), 'hi', owner=False)
    assert not any(name.startswith('files_') for name in model.tool_names[0])
