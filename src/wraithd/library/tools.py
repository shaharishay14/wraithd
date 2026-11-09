"""
The library tools, for the owner's runs only (chat, schedules, watches): library_seen, library_save, library_search,
idea_save, idea_list, idea_status, and wraithd_index.

All are READ tier in the gate: the writes only change wraithd's own notes in state.db, never the owner's data or the
outside world, so an unattended radar run never waits for an approval. Each write is logged as an event between
runs (library_saved, idea_saved, idea_status). library_search and idea_list return notes made from posts and pages:
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

SEEN, SAVE, SEARCH = 'library_seen', 'library_save', 'library_search'
IDEA_SAVE, IDEA_LIST, IDEA_STATUS = 'idea_save', 'idea_list', 'idea_status'
INDEX = 'wraithd_index'
NAMES = (SEEN, SAVE, SEARCH, IDEA_SAVE, IDEA_LIST, IDEA_STATUS, INDEX)

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

    async def idea_save(title: str, plan: str, size: str, files: list[str] | None = None,
                        source_ids: list[str] | None = None) -> dict:
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

    async def idea_list(status: str = '', limit: int = 50) -> dict:
        try:
            ideas = library.ideas(status or None, clamp_int(limit, 1, 200, 50))
        except LibraryError as e:
            return tool_error(str(e), 'Leave status out for every idea.')
        return {'ideas': [idea.shown() for idea in ideas]}

    async def idea_status(id: str, status: str) -> dict:
        try:
            idea, previous = library.set_status(id, status)
        except LibraryError as e:
            return tool_error(str(e), f'Call {IDEA_LIST} for the ids.')
        ev.record_event(store, ev.IDEA_STATUS, id=idea.id, status=idea.status, previous=previous)
        logger.info(f'Library: idea {idea.id} {previous} -> {idea.status}')
        return {'idea': idea.id, 'status': idea.status}

    async def wraithd_index() -> str:
        return index.build()

    text = {'type': 'string'}
    strings = {'type': 'array', 'items': text}
    return [
        function_tool(
            SEEN,
            f'Which of these items were already read (one call for all): ids {ID_FORMAT}. Returns the normalized '
            'ids as new or seen. Read only the new ones.',
            {'properties': {'ids': strings}, 'required': ['ids']}, seen),
        function_tool(
            SAVE,
            f'Keep an item you read, with a short note in your own words (at most {NOTE_CHARS} characters: what it '
            'is, what matters for wraithd, names and numbers to find it again).',
            {'properties': {'id': {**text, 'description': ID_FORMAT}, 'kind': {'enum': list(KINDS)}, 'title': text,
                            'url': text, 'note': text, 'author': text, 'source': {'enum': list(SOURCES)}},
             'required': ['id', 'kind', 'title', 'url', 'note', 'source']},
            save),
        function_tool(
            SEARCH,
            'Search the items read before by words in their title, author, note or url, best first. Answer questions '
            'about something read earlier from its note; re-read it only if the note doesn\'t cover it.',
            {'properties': {'query': text, 'limit': {'type': 'integer', 'description': 'Default 5'}},
             'required': ['query']},
            search),
        function_tool(
            IDEA_SAVE,
            f'Keep an idea suggested to the user: a plan of at most {PLAN_CHARS} characters, the real files it '
            'touches, size S/M/L and the ids of the items it came from. Returns its id (date#n). One suggested '
            'before is not added again.',
            {'properties': {'title': text, 'plan': text, 'files': strings, 'size': {'enum': list(SIZES)},
                            'source_ids': strings},
             'required': ['title', 'plan', 'size']},
            idea_save),
        function_tool(
            IDEA_LIST,
            'The ideas suggested before, newest first, with their status.',
            {'properties': {'status': {'enum': list(STATUSES)}, 'limit': {'type': 'integer'}}},
            idea_list),
        function_tool(
            IDEA_STATUS,
            'Set an idea\'s status when the user accepts or declines it, or it is built.',
            {'properties': {'id': {**text, 'description': 'e.g. 2026-11-09#3'}, 'status': {'enum': list(STATUSES)}},
             'required': ['id', 'status']},
            idea_status),
        function_tool(
            INDEX,
            'What wraithd is today: its modules, its notes (docs) with one line each, and the latest changelog.',
            {'properties': {}}, wraithd_index),
    ]
