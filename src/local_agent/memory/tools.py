"""
The agent's memory tools: memory_search (L2), memory_read (L3), memory_append and memory_write.

They go through the permission gate like every tool. Two rules are enforced here, in code:
- memory_write replaces a note, so the note must have been read in this run first
- anything written after third-party content entered the run is stored as low trust, and is shown back to the
  model as third-party data, never as instructions
"""

import contextlib
import difflib
import json

from agents import FunctionTool

from local_agent.core import events as ev
from local_agent.core.gate import parse_arguments
from local_agent.core.secrets import redact_text
from local_agent.memory import notes as note_files
from local_agent.memory import vault
from local_agent.memory.notes import Note, Vault

SEARCH, READ, APPEND, WRITE = 'memory_search', 'memory_read', 'memory_append', 'memory_write'
NAMES = (SEARCH, READ, APPEND, WRITE)
READ_CHARS = 8000


def _error(message: str, hint: str = '', **extra) -> str:
    return json.dumps({'error': message, **extra, **({'hint': hint} if hint else {})})


def read_paths(events) -> set[str]:
    """Notes the model has read with memory_read in this run."""
    paths = set()
    for event in events:
        if event.type == ev.TOOL_CALL and event.payload.get('tool') == READ and event.payload.get('allowed'):
            with contextlib.suppress(ValueError):
                paths.add(vault.normalize_path(event.payload.get('arguments', {}).get('path', '')))
    return paths


def memory_tools(memory: Vault, gate, log: ev.EventLog) -> list[FunctionTool]:
    """The four tools for one run. gate tells whether third-party content has entered the run."""

    def provenance():
        """Trust and source of a write made now: low as soon as the run has read third-party content."""
        detail = f'run {log.run_id}'
        if gate.untrusted_sources:
            return vault.LOW, f'{detail}, after reading {", ".join(gate.untrusted_sources)}'
        return vault.HIGH, detail

    async def search(ctx, input_json: str) -> str:
        results = vault.search(memory.load(), parse_arguments(input_json).get('query', ''))
        if not results:
            return json.dumps({'results': [], 'hint': 'Nothing matched. Try other words (see the folder keywords), '
                                                      'or ask the user.'})
        return json.dumps({
            'results': results,
            # a low-trust note in the results makes this output third-party data for the gate
            'untrusted': any(result['trust'] == vault.LOW for result in results),
            'hint': f'Call {READ} with a path to see the full note.',
        })

    async def read(ctx, input_json: str) -> str:
        try:
            path = vault.normalize_path(parse_arguments(input_json).get('path', ''))
        except ValueError as e:
            return _error(str(e))
        all_notes = memory.load()
        note = next((n for n in all_notes if n.path == path), None)
        if note is None:
            return _error(f'There is no note {path}.', f'Use {SEARCH} to find notes.',
                          did_you_mean=difflib.get_close_matches(path, [n.path for n in all_notes], n=3, cutoff=0.5))
        body = note.body or ''
        linked_from = sorted(n.path for n in all_notes if n.path != path and
                             any(link.removesuffix('.md') in (path.removesuffix('.md'), vault.stem_of(path))
                                 for link in n.links))
        return json.dumps({
            'path': note.path, 'title': note.title,
            'body': body if len(body) <= READ_CHARS else body[:READ_CHARS] + '\n[TRUNCATED]',
            'links': note.links, 'linked_from': linked_from,
            'source': note.source, 'source_detail': note.source_detail, 'updated_at': note.updated_at.isoformat(),
            'untrusted': note.trust == vault.LOW,
        })

    def _save(path, title, body, append):
        trust, detail = provenance()
        note = memory.get(path)
        created = note is None
        if created:
            note = Note(path=path, title=title or note_files._default_title(path))
        body = redact_text(body)  # a secret the agent saw doesn't become a permanent note
        note.body = (note.body.rstrip() + '\n\n' + body.strip()).strip() if append and not created else body.strip()
        if title:
            note.title = title
        note.source, note.source_detail = note_files.SOURCE_AGENT, detail
        # A new or replaced note takes this write's trust. Appending low-trust text makes the whole note low trust
        # (the text can't be told apart afterwards); appending trusted text doesn't clean a low one.
        if created or not append or trust == vault.LOW:
            note.trust = trust
        memory.save(note)
        return note, created

    async def append(ctx, input_json: str) -> str:
        arguments = parse_arguments(input_json)
        text = (arguments.get('text') or '').strip()
        try:
            path = vault.normalize_path(arguments.get('path', ''))
        except ValueError as e:
            return _error(str(e))
        if not text:
            return _error('Nothing to save: "text" is empty.')
        note, created = _save(path, arguments.get('title'), text, append=True)
        return json.dumps({'saved': note.path, 'created': created, 'trust': note.trust})

    async def write(ctx, input_json: str) -> str:
        arguments = parse_arguments(input_json)
        try:
            path = vault.normalize_path(arguments.get('path', ''))
        except ValueError as e:
            return _error(str(e))
        if memory.get(path) is not None and path not in read_paths(log.events):
            return _error(f'{path} was not read in this run, so it can\'t be replaced.',
                          f'Call {READ} on it first, then write the full new version (or use {APPEND} to add to it).')
        note, created = _save(path, arguments.get('title'), arguments.get('body') or '', append=False)
        return json.dumps({'saved': note.path, 'created': created, 'trust': note.trust})

    path_description = f'folder/name, folder one of {", ".join(vault.FOLDERS)}, e.g. "people/dana.md"'
    return [
        FunctionTool(
            name=SEARCH,
            description='Search your memory of the user (notes from earlier conversations). Returns matching lines '
                        'only.',
            params_json_schema={'type': 'object', 'properties': {
                'query': {'type': 'string', 'description': 'A few words, e.g. "dana designer", "report format"'}},
                'required': ['query']},
            on_invoke_tool=search, strict_json_schema=False),
        FunctionTool(
            name=READ,
            description='Read one memory note in full, with the notes it links to and that link to it.',
            params_json_schema={'type': 'object', 'properties': {
                'path': {'type': 'string', 'description': path_description}}, 'required': ['path']},
            on_invoke_tool=read, strict_json_schema=False),
        FunctionTool(
            name=APPEND,
            description=('Remember a durable fact about the user: adds it to a note (creating the note if needed). '
                         'Link related notes with [[name]], e.g. "Works with [[dana]] on [[website-redesign]]".'),
            params_json_schema={'type': 'object', 'properties': {
                'path': {'type': 'string', 'description': path_description},
                'text': {'type': 'string', 'description': 'The fact, in a short sentence or bullet'},
                'title': {'type': 'string', 'description': 'Optional title for a new note'}},
                'required': ['path', 'text']},
            on_invoke_tool=append, strict_json_schema=False),
        FunctionTool(
            name=WRITE,
            description=(f'Replace a whole memory note, e.g. to correct or tidy it. Read it with {READ} first. '
                         f'To add a fact, use {APPEND} instead.'),
            params_json_schema={'type': 'object', 'properties': {
                'path': {'type': 'string', 'description': path_description},
                'body': {'type': 'string', 'description': 'The full new markdown of the note'},
                'title': {'type': 'string'}},
                'required': ['path', 'body']},
            on_invoke_tool=write, strict_json_schema=False),
    ]
