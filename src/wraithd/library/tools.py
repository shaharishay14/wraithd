"""
The library tools, for the owner's runs only (chat, schedules, watches): library_seen, library_save, library_search,
ideas (action list, save or status) and wraithd_index. Five tools: 534 tokens of schemas (o200k) on every owner call.

All are READ tier in the gate: the writes only change wraithd's own notes in state.db, never the owner's data or the
outside world, so an unattended radar run never waits for an approval. Each write is logged as an event between
runs (library_saved, idea_saved, idea_status). library_search and ideas return notes made from posts and pages:
third-party data, so they taint the run.
"""

import logging
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from wraithd.core import events as ev
from wraithd.library import (
    KINDS,
    MAX_IDS,
    NOTE_CHARS,
    PLAN_CHARS,
    SIZES,
    SOURCES,
    STATUSES,
    Library,
    LibraryError,
    index,
)
from wraithd.tools.base import clamp_int, function_tool, tool_error

SEEN, SAVE, SEARCH, IDEAS, INDEX = 'library_seen', 'library_save', 'library_search', 'ideas', 'wraithd_index'
NAMES = (SEEN, SAVE, SEARCH, IDEAS, INDEX)
ACTIONS = ('list', 'save', 'status')

ID_FORMAT = 'x:<post id>, gh:<owner>/<repo> or url:<url>'

logger = logging.getLogger(__name__)


def _today(timezone: str, clock) -> date:
    try:
        return clock().astimezone(ZoneInfo(timezone)).date()
    except (ZoneInfoNotFoundError, ValueError):
        return clock().astimezone().date()


def _list(value) -> list:
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


def library_tools(store, timezone: str, clock=lambda: datetime.now(UTC)) -> list:
    library = Library(store)

    async def seen(ids: list[str]) -> dict:
        ids = _list(ids)
        if not ids:
            return tool_error('No ids given.', f'Pass the ids of the items you found: {ID_FORMAT}.')
        if len(ids) > MAX_IDS:
            return tool_error(f'{len(ids)} ids is more than {MAX_IDS} at once.', 'Check them in smaller batches.')
        return library.seen(ids)

    async def save(id: str, kind: str, title: str, url: str = '', note: str = '', source: str = '',
                   author: str = '') -> dict:
        try:
            item = library.save(id, kind, title, url, note, source, author)
        except LibraryError as e:
            return tool_error(str(e), 'Fix it and save again.')
        ev.record_event(store, ev.LIBRARY_SAVED, id=item.id, kind=item.kind, source=item.source, title=item.title)
        logger.info(f'Library: saved {item.id}')
        result = {'saved': item.id}
        if len(str(note or '')) > NOTE_CHARS:
            result['hint'] = f'The note was cut to {NOTE_CHARS} characters.'
        return result

    async def search(query: str = '', limit: int = 5) -> dict:
        items = library.search(query, clamp_int(limit, 1, 20, 5))
        return {'results': [item.shown() for item in items]}

    async def idea_save(title, plan, size, files, source_ids) -> dict:
        try:
            idea, added = library.add_idea(title, plan, _list(files), size, _list(source_ids),
                                           _today(timezone, clock))
        except LibraryError as e:
            return tool_error(str(e), 'Fix it and save again.')
        if not added:
            return {'idea': idea.id, 'already': idea.status,
                    'hint': f'This idea was suggested before ({idea.id}, {idea.status}). Don\'t suggest it again.'}
        ev.record_event(store, ev.IDEA_SAVED, id=idea.id, title=idea.title, size=idea.size)
        logger.info(f'Library: idea {idea.id} saved')
        result = {'idea': idea.id}
        if len(str(plan or '')) > PLAN_CHARS:
            result['hint'] = f'The plan was cut to {PLAN_CHARS} characters.'
        return result

    async def idea_status(idea_id, status) -> dict:
        try:
            idea, previous = library.set_status(idea_id, status)
        except LibraryError as e:
            return tool_error(str(e), 'Call ideas with action "list" for the ids.')
        ev.record_event(store, ev.IDEA_STATUS, id=idea.id, status=idea.status, previous=previous)
        logger.info(f'Library: idea {idea.id} {previous} -> {idea.status}')
        return {'idea': idea.id, 'status': idea.status}

    async def ideas(action: str = 'list', id: str = '', title: str = '', plan: str = '', size: str = '',
                    files: list[str] | None = None, source_ids: list[str] | None = None, status: str = '',
                    limit: int = 50) -> dict:
        if action == 'save':
            return await idea_save(title, plan, size, files, source_ids)
        if action == 'status':
            return await idea_status(id, status)
        if action != 'list':
            return tool_error(f'action {action!r} is not one of {", ".join(ACTIONS)}.', 'Use list, save or status.')
        try:
            found = library.ideas(status or None, clamp_int(limit, 1, 200, 50))
        except LibraryError as e:
            return tool_error(str(e), 'Leave status out for every idea.')
        return {'ideas': [idea.shown() for idea in found]}

    async def wraithd_index() -> str:
        return index.build()

    text = {'type': 'string'}
    strings = {'type': 'array', 'items': text}
    return [
        function_tool(SEEN, f'Which item ids ({ID_FORMAT}) were read before. Read only the new ones.',
                      {'properties': {'ids': strings}, 'required': ['ids']}, seen),
        function_tool(
            SAVE, f'Keep an item read, with a note in your own words (max {NOTE_CHARS} chars).',
            {'properties': {'id': text, 'kind': {'enum': list(KINDS)}, 'title': text, 'url': text, 'note': text,
                            'author': text, 'source': {'enum': list(SOURCES)}},
             'required': ['id', 'kind', 'title', 'note', 'source']},
            save),
        function_tool(SEARCH, 'Search items read before; answer from the note, re-read only if it falls short.',
                      {'properties': {'query': text, 'limit': {'type': 'integer'}}, 'required': ['query']}, search),
        function_tool(
            IDEAS,
            f'Ideas suggested to the user. list (by status); save: title, plan (max {PLAN_CHARS} chars), size, '
            'files, source_ids, returns id date#n, never twice; status: id and status.',
            {'properties': {'action': {'enum': list(ACTIONS)}, 'id': text, 'title': text, 'plan': text,
                            'size': {'enum': list(SIZES)}, 'files': strings, 'source_ids': strings,
                            'status': {'enum': list(STATUSES)}, 'limit': {'type': 'integer'}},
             'required': ['action']},
            ideas),
        function_tool(INDEX, "wraithd's modules, docs and latest changelog: what is already built.",
                      {'properties': {}}, wraithd_index),
    ]
