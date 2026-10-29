"""
MCP tool pinning: a server can change what a tool says it is after the owner trusted it. A tool the owner
always-allowed as "add a label" can turn into "forward the inbox" with the same name; a send can start calling
itself read-only. So every tool's definition (name, description, input schema, annotations) is hashed on every
connect and compared with what the owner last accepted:

- "always allow" holds only for the exact definition it was given to (the pin). A tool always-allowed before
  pinning existed has no pin: it asks until the owner allows it again ("re-allow needed").
- A changed tool loses its pre-approval, logs mcp_tool_changed, and asks again. Its tier is the stricter of the old
  and the new annotations, so a read-only flip or a dropped destructiveHint doesn't loosen the gate. It goes to the
  review queue with the old and new definitions; approving accepts the new one (and restores an allow it had).
- A tool that appears on a server already known asks, even if it says it's read-only, until the owner reviews it.
- A server's first connect is the baseline: its tools are accepted as they are (trust on first use).

The two picks that decide the upgrade and the first connect are FIRST_CONNECT_IS_BASELINE and the D1 rule in
_allowed (an unpinned always-allow asks); each is one line.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from local_agent.core import events as ev
from local_agent.review import queue
from local_agent.store.modules import ModuleTables

logger = logging.getLogger(__name__)

KIND = 'mcp_tool'
OK, CHANGED, NEW, REALLOW = 'ok', 'changed', 'new', 'reallow'
FIRST_CONNECT_IS_BASELINE = True  # False would queue every tool of a new server for review
LABELS = {CHANGED: 'changed: asks until you review it', NEW: 'new: asks until you review it',
          REALLOW: 're-allow needed'}


class PinError(ValueError):
    """The tool can't be pinned now. The message says what to do."""


@dataclass(frozen=True)
class Verdict:
    """What a connect decided about one tool."""

    read_only: bool
    destructive: bool
    allowed: bool  # always-allowed, and the pin matches
    state: str = OK  # ok | changed | new | reallow
    review_id: int | None = None


def definition(tool) -> dict[str, Any]:
    """What the server says the tool is, as plain JSON."""
    annotations = getattr(tool, 'annotations', None)
    if annotations is not None and hasattr(annotations, 'model_dump'):
        annotations = annotations.model_dump(by_alias=True, exclude_none=True)
    return {'name': tool.name, 'description': tool.description or '', 'input_schema': tool.input_schema or {},
            'annotations': annotations or {}}


def digest(found: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(found, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def _read_only(found: dict) -> bool:
    return found['annotations'].get('readOnlyHint') is True


def _destructive(found: dict) -> bool:
    # Only an explicit hint counts (mcp/client.py is_destructive)
    return found['annotations'].get('destructiveHint') is True


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec='microseconds')


class Pins(ModuleTables):
    def row(self, server: str, tool: str) -> dict | None:
        rows = self.query('SELECT * FROM security_mcp_tools WHERE server = ? AND tool = ?', (server, tool))
        return rows[0] if rows else None

    def rows(self, server: str | None = None) -> list[dict]:
        if server is None:
            return self.query('SELECT * FROM security_mcp_tools ORDER BY server, tool')
        return self.query('SELECT * FROM security_mcp_tools WHERE server = ? ORDER BY tool', (server,))

    def known(self, server: str) -> bool:
        return bool(self.query('SELECT 1 FROM security_mcp_tools WHERE server = ? LIMIT 1', (server,)))

    def accept(self, server: str, tool: str, found: dict) -> None:
        """found is now the reviewed definition (and the one seen)."""
        hashed, text = digest(found), json.dumps(found)
        self.execute(
            'INSERT INTO security_mcp_tools (server, tool, reviewed_hash, reviewed_definition, seen_hash, '
            'seen_definition, seen_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (server, tool) DO UPDATE SET '
            'reviewed_hash = excluded.reviewed_hash, reviewed_definition = excluded.reviewed_definition, '
            'seen_hash = excluded.seen_hash, seen_definition = excluded.seen_definition, seen_at = excluded.seen_at',
            (server, tool, hashed, text, hashed, text, _now()))

    def see(self, server: str, tool: str, found: dict) -> None:
        self.execute(
            'INSERT INTO security_mcp_tools (server, tool, seen_hash, seen_definition, seen_at) VALUES (?, ?, ?, ?, ?) '
            'ON CONFLICT (server, tool) DO UPDATE SET seen_hash = excluded.seen_hash, '
            'seen_definition = excluded.seen_definition, seen_at = excluded.seen_at',
            (server, tool, digest(found), json.dumps(found), _now()))

    def set(self, server: str, tool: str, **values) -> None:
        columns = ', '.join(f'{name} = ?' for name in values)
        self.execute(f'UPDATE security_mcp_tools SET {columns} WHERE server = ? AND tool = ?',
                     (*values.values(), server, tool))


def _queue(store, server: str, tool: str, found: dict, old: dict | None, loosened: bool) -> int:
    hashed = digest(found)
    what = 'New tool' if old is None else 'Changed tool'
    item = queue.ReviewQueue(store).add(
        KIND, {'server': server, 'tool': tool, 'hash': hashed, 'old': old, 'new': found, 'loosened': loosened},
        f'{what} {server} {tool}' + (': now claims fewer risks' if loosened else ''), 'mcp',
        taint=[f'MCP server {server}'], key=f'{server}/{tool}/{hashed}')
    return item.id


def check(store, server, listed) -> dict[str, Verdict]:
    """
    Compares each listed tool with what the owner accepted, records what was seen, logs and queues changes.
    server is the configured McpServer (its always_allow list). Returns a verdict per tool, by the server's name for it.
    """
    pins = Pins(store)
    baseline = FIRST_CONNECT_IS_BASELINE and not pins.known(server.name)
    verdicts = {}
    for tool in listed:
        found = definition(tool)
        hashed = digest(found)
        row = pins.row(server.name, tool.name)
        if row is None and baseline:
            pins.accept(server.name, tool.name, found)
            row = pins.row(server.name, tool.name)
        previous_seen = row['seen_hash'] if row else None
        if row is None or row['reviewed_hash'] is None:  # never reviewed: a tool that appeared later
            pins.see(server.name, tool.name, found)
            if previous_seen != hashed:
                ev.record_event(store, ev.MCP_TOOL_NEW, server=server.name, tool=tool.name, hash=hashed[:12])
            review_id = (_queue(store, server.name, tool.name, found, None, _read_only(found))
                         if (row or {}).get('rejected_hash') != hashed else None)
            verdicts[tool.name] = Verdict(read_only=False, destructive=_destructive(found), allowed=False, state=NEW,
                                          review_id=review_id)
            continue

        reviewed = json.loads(row['reviewed_definition'])
        if hashed == row['reviewed_hash']:
            pins.see(server.name, tool.name, found)
            listed_allowed = tool.name in server.always_allow
            allowed = listed_allowed and row['allowed_hash'] == hashed  # D1: an unpinned allow asks
            verdicts[tool.name] = Verdict(read_only=_read_only(found), destructive=_destructive(found),
                                          allowed=allowed, state=REALLOW if listed_allowed and not allowed else OK)
            continue

        # Changed since the owner accepted it: the stricter of the two, and no pre-approval
        pins.see(server.name, tool.name, found)
        read_only = _read_only(reviewed) and _read_only(found)
        destructive = _destructive(reviewed) or _destructive(found)
        loosened = _read_only(found) and not _read_only(reviewed) or _destructive(reviewed) and not _destructive(found)
        changes = [part for part in ('description', 'input_schema', 'annotations') if reviewed[part] != found[part]]
        if previous_seen != hashed:
            ev.record_event(store, ev.MCP_TOOL_CHANGED, server=server.name, tool=tool.name, changes=changes,
                            loosened=loosened, was_allowed=row['allowed_hash'] is not None, hash=hashed[:12])
        review_id = (_queue(store, server.name, tool.name, found, reviewed, loosened)
                     if row['rejected_hash'] != hashed else None)
        verdicts[tool.name] = Verdict(read_only=read_only, destructive=destructive, allowed=False, state=CHANGED,
                                      review_id=review_id)
    return verdicts


def pin(store, server: str, tool: str, by: str = 'cli') -> str:
    """
    Always-allow this tool as the server showed it on the last connect. Refused for a tool that was never seen, or
    that is new or changed and not reviewed yet (the owner reviews it first, then allows it).
    """
    pins = Pins(store)
    row = pins.row(server, tool)
    if row is None:
        raise PinError(f'{server} has no tool {tool} (as of its last connect).')
    if row['reviewed_hash'] is None or row['reviewed_hash'] != row['seen_hash']:
        raise PinError(f'{server} {tool} is new or changed since you last accepted it. Review it first: '
                       'local-agent review list')
    pins.set(server, tool, allowed_hash=row['reviewed_hash'])
    ev.record_event(store, ev.MCP_TOOL_PINNED, server=server, tool=tool, by=by, hash=row['reviewed_hash'][:12])
    return row['reviewed_hash']


def unpin(store, server: str, tool: str) -> None:
    Pins(store).set(server, tool, allowed_hash=None)


def forget(store, server: str) -> None:
    """A removed server: added again, it starts from a new baseline."""
    Pins(store).execute('DELETE FROM security_mcp_tools WHERE server = ?', (server,))


# --- the review queue's mcp_tool kind --------------------------------------------------------------------------

def approve(item: queue.ReviewItem, ctx: queue.ReviewContext) -> str:
    content = item.content
    pins = Pins(ctx.store)
    row = pins.row(content['server'], content['tool'])
    if row is None or row['seen_hash'] != content['hash']:
        raise queue.ReviewError(f'{content["server"]} changed {content["tool"]} again since this was queued; review '
                                'the newer item instead.')
    restore = row['allowed_hash'] is not None and not _destructive(content['new'])
    pins.accept(content['server'], content['tool'], content['new'])
    pins.set(content['server'], content['tool'], rejected_hash=None,
             allowed_hash=content['hash'] if restore else None)
    if restore:
        ev.record_event(ctx.store, ev.MCP_TOOL_PINNED, server=content['server'], tool=content['tool'], by='review',
                        hash=content['hash'][:12])
    return (f'accepted {content["server"]} {content["tool"]} as it is now'
            + (', still always allowed' if restore else '') + ' (from its next connect)')


def reject(item: queue.ReviewItem, ctx: queue.ReviewContext) -> None:
    """The tool stays held at its stricter tier, and this definition isn't queued again."""
    Pins(ctx.store).set(item.content['server'], item.content['tool'], rejected_hash=item.content['hash'])


def show(item: queue.ReviewItem) -> str:
    content, lines = item.content, []
    old, new = content.get('old'), content['new']
    what = 'New tool' if old is None else 'Changed tool'
    lines.append(f'{what} {content["tool"]} on MCP server {content["server"]}.')
    if content.get('loosened'):
        lines.append('It now claims to be less risky (read-only, or no longer destructive). Until you approve, it '
                     'keeps its stricter tier.')
    for part in ('description', 'input_schema', 'annotations'):
        if old is None or old[part] != new[part]:
            if old is not None:
                lines.append(f'\n{part} before:\n{json.dumps(old[part], indent=2, ensure_ascii=False)}')
            lines.append(f'\n{part}{" now" if old is not None else ""}:\n'
                         f'{json.dumps(new[part], indent=2, ensure_ascii=False)}')
    return '\n'.join(lines)


queue.register(KIND, approve, reject=reject, show=show)
