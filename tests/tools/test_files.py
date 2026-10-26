"""
Workspace files: list, read and write inside workspace/files/, and nothing outside it, however the path is
written (`..`, absolute, `~`, or a symlink inside the folder that points out). Through the real run().
"""

import json
import os

import pytest

from local_agent.core import events as ev
from local_agent.tools import files as files_module
from local_agent.tools.files import OutsideWorkspace, file_tools, resolve_inside
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


# --- files_edit --------------------------------------------------------------------------------------------

PLAN = '# Trip\n\n## Flights\nTLV -> LIS, 9 May\n\n## Hotel\nnot booked\n\n## Notes\n- pack light\n'


async def edit(root, **arguments):
    return await call_tool(by_name(root)['files_edit'], **arguments)


async def test_an_edit_changes_one_section_and_leaves_the_rest_byte_for_byte(files):
    plan = files / 'plan.md'
    plan.write_bytes(PLAN.encode())
    plan.chmod(0o640)
    result = await edit(files, path='plan.md', old_text='## Hotel\nnot booked\n',
                        new_text='## Hotel\nCasa do Rio, 9-14 May\nconfirmation 4471\n')
    after = plan.read_bytes()
    before = PLAN.encode()
    start = before.index(b'## Hotel')
    assert after[:start] == before[:start]  # everything before the section, unchanged
    assert after.endswith(before[before.index(b'\n## Notes'):])  # and everything after it
    assert after == before.replace(b'not booked', b'Casa do Rio, 9-14 May\nconfirmation 4471')
    assert result == {'path': 'plan.md', 'replacements': 1, 'bytes': len(after),
                      'context': '4| TLV -> LIS, 9 May\n5| \n6| ## Hotel\n7| Casa do Rio, 9-14 May\n'
                                 '8| confirmation 4471\n9| \n10| ## Notes'}
    assert (plan.stat().st_mode & 0o777) == 0o640  # the file's mode is kept
    assert [p.name for p in files.iterdir() if p.name.endswith('.tmp')] == []


async def test_text_that_isnt_there_or_is_there_twice_changes_nothing(files):
    (files / 'plan.md').write_text(PLAN)
    missing = await edit(files, path='plan.md', old_text='## Hotels', new_text='x')
    assert 'not found' in missing['error'] and 'files_read' in missing['error'] and 'spaces' in missing['error']
    twice = await edit(files, path='plan.md', old_text='\n\n## ', new_text='\n## ')
    assert 'found 3 times' in twice['error'] and 'replace_all' in twice['error']
    empty = await edit(files, path='plan.md', old_text='', new_text='x')
    assert 'old_text is empty' in empty['error']
    assert (files / 'plan.md').read_text() == PLAN


async def test_replace_all_and_delete(files):
    (files / 'plan.md').write_text(PLAN)
    every = await edit(files, path='plan.md', old_text='\n\n## ', new_text='\n## ', replace_all=True)
    assert every['replacements'] == 3
    assert (files / 'plan.md').read_text() == PLAN.replace('\n\n## ', '\n## ')
    gone = await edit(files, path='plan.md', old_text='## Notes\n- pack light\n', new_text='')
    assert gone['replacements'] == 1 and not (files / 'plan.md').read_text().count('Notes')


async def test_windows_line_endings_and_unicode_are_kept(files):
    note = files / 'notes' / 'win.txt'
    note.write_bytes('שלום עולם\r\ncafé ☕\r\nlast line\r\n'.encode())
    # the model writes plain newlines; the file's own CRLF are matched and kept
    result = await edit(files, path='notes/win.txt', old_text='café ☕\nlast', new_text='café ☕ (closed)\nfinal')
    assert result['replacements'] == 1
    assert note.read_bytes() == 'שלום עולם\r\ncafé ☕ (closed)\r\nfinal line\r\n'.encode()
    exact = await edit(files, path='notes/win.txt', old_text='שלום', new_text='Hello')
    assert exact['replacements'] == 1 and note.read_bytes().startswith(b'Hello \xd7\xa2')


async def test_documents_binary_and_non_utf8_files_are_refused(files):
    (files / 'report.pdf').write_bytes(b'%PDF-1.4 text')
    (files / 'photo.jpg').write_bytes(b'\xff\xd8\x00\x10JFIF')
    (files / 'latin1.txt').write_bytes('caf\xe9'.encode('latin-1'))
    document = await edit(files, path='report.pdf', old_text='text', new_text='x')
    assert 'is a document' in document['error'] and 'write a new file' in document['error']
    assert 'binary' in (await edit(files, path='photo.jpg', old_text='JFIF', new_text='x'))['error']
    assert 'not UTF-8' in (await edit(files, path='latin1.txt', old_text='caf', new_text='x'))['error']
    assert (files / 'latin1.txt').read_bytes() == b'caf\xe9'
    assert 'no file' in (await edit(files, path='nope.md', old_text='a', new_text='b'))['error']


async def test_files_too_large_to_edit_are_refused(files, monkeypatch):
    monkeypatch.setattr(files_module, 'MAX_WRITE_CHARS', 100)
    (files / 'big.txt').write_text('x' * 150)
    assert 'too large to edit' in (await edit(files, path='big.txt', old_text='x', new_text='y',
                                              replace_all=True))['error']
    (files / 'small.txt').write_text('x' * 90)
    assert 'would be too large' in (await edit(files, path='small.txt', old_text='x', new_text='yy',
                                               replace_all=True))['error']
    assert (files / 'small.txt').read_text() == 'x' * 90


async def test_a_failed_write_leaves_the_original_intact(files, monkeypatch):
    (files / 'plan.md').write_text(PLAN)

    def failing(*args, **kwargs):
        raise OSError(28, 'No space left on device')
    monkeypatch.setattr(files_module.os, 'replace', failing)
    result = await edit(files, path='plan.md', old_text='not booked', new_text='booked')
    monkeypatch.undo()
    assert 'left as it was' in result['error']
    assert (files / 'plan.md').read_text() == PLAN
    assert [p.name for p in files.iterdir() if p.name.endswith('.tmp')] == []


async def test_edits_outside_the_folder_or_through_a_swapped_file_change_nothing(files, outside, after_the_check):
    (files / 'escape.txt').symlink_to(outside)
    for path in ['../outside/secret.txt', 'escape.txt', str(outside)]:
        result = await edit(files, path=path, old_text='ssh key', new_text='pwned')
        assert 'error' in result and 'ssh key' not in json.dumps(result)
    assert outside.read_text() == 'the owner\'s ssh key'

    after_the_check(lambda: swap_for_link(files / 'notes' / 'todo.md', outside))
    swapped = await edit(files, path='notes/todo.md', old_text='ssh key', new_text='pwned')
    assert 'changed while it was being opened' in swapped['error'] and outside.read_text() == 'the owner\'s ssh key'


async def test_an_edit_into_a_folder_swapped_after_the_check_changes_nothing(files, decoy, after_the_check):
    after_the_check(lambda: swap_for_link(files / 'notes', decoy))
    folder = await edit(files, path='notes/todo.md', old_text='ssh key', new_text='pwned')
    assert 'changed' in folder['error'] and (decoy / 'todo.md').read_text() == 'the owner\'s ssh key'


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
