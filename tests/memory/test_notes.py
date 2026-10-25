"""
Notes as markdown files: what the harness writes, what it reads back (including files the owner wrote by hand),
and that a path from the model can't leave the memory folder.
"""

import os

import pytest

from local_agent.memory import vault
from local_agent.memory.notes import SOURCE_AGENT, SOURCE_USER, Note, Vault


@pytest.fixture
def memory(tmp_path):
    return Vault(tmp_path / 'memory')


def test_a_saved_note_reads_back_with_its_trust_and_source(memory):
    memory.save(Note(path='people/dana.md', title='Dana', body='- Designer at Acme, see [[website-redesign]]',
                     trust=vault.LOW, source=SOURCE_AGENT, source_detail='run 1, after reading web_search'))
    note = memory.get('people/dana.md')
    assert (note.title, note.body, note.trust, note.source) == ('Dana', '- Designer at Acme, see [[website-redesign]]',
                                                               'low', SOURCE_AGENT)
    assert note.source_detail == 'run 1, after reading web_search' and note.links == ['website-redesign']
    text = (memory.root / 'people' / 'dana.md').read_text()
    assert text.startswith('---\ntitle: Dana\n') and 'trust: low' in text and text.endswith('[[website-redesign]]\n')


def test_a_hand_written_note_is_the_owners_and_trusted(memory):
    (memory.root / 'preferences').mkdir(parents=True)
    (memory.root / 'preferences' / 'writing.md').write_text('# Writing\nShort emails, no emojis.\n')
    [note] = memory.load()
    assert (note.path, note.title, note.trust, note.source) == ('preferences/writing.md', 'Writing', 'high',
                                                                SOURCE_USER)


def test_deleting_the_trust_line_vouches_for_a_note(memory):
    memory.save(Note(path='people/eve.md', title='Eve', body='Accountant', trust=vault.LOW, source=SOURCE_AGENT))
    file = memory.root / 'people' / 'eve.md'
    file.write_text(file.read_text().replace('trust: low\n', ''))
    assert memory.get('people/eve.md').trust == vault.HIGH


def test_files_outside_the_schema_are_ignored(memory):
    for path in ['secrets/keys.md', 'people/My Notes.md', 'people/notes.txt', 'loose.md']:
        file = memory.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text('x')
    assert memory.load() == []


def test_a_path_from_the_model_cannot_leave_the_memory_folder(memory, tmp_path):
    for bad in ['../../etc/passwd', '/tmp/x', 'people/', 'secrets/x']:
        with pytest.raises(ValueError):
            memory.save(Note(path=bad, title='x', body='x'))
    # what's left of a sneaky name stays inside, flattened to one kebab-case file name
    memory.save(Note(path='people/../../outside', title='x', body='x'))
    assert vault.normalize_path('people/..%2F..%2Fx') == 'people/2f-2fx.md'
    assert [path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob('*.md')] == ['memory/people/outside.md']


def test_notes_load_newest_first_and_broken_frontmatter_is_just_text(memory):
    memory.save(Note(path='people/a.md', title='A', body='old'))
    memory.save(Note(path='people/b.md', title='B', body='new'))
    os.utime(memory.root / 'people' / 'a.md', (1, 1))
    (memory.root / 'projects').mkdir()
    (memory.root / 'projects' / 'x.md').write_text('---\n: : not yaml [\n---\nbody')
    loaded = memory.load()
    assert loaded[-1].path == 'people/a.md'  # the oldest last
    broken = next(note for note in loaded if note.path == 'projects/x.md')
    assert 'not yaml' in broken.body and broken.trust == vault.HIGH
