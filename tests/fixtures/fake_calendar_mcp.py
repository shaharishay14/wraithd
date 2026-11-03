"""
A stand-in for @cocal/google-calendar-mcp (2.8.1) for the first-launch test, run as a real stdio process:

    python fake_calendar_mcp.py start --enable-tools list-events,create-event --store PATH

Same tool names and the same annotations as the real server (reads readOnlyHint, create-event a plain write,
update-event and delete-event destructiveHint), and like it: --enable-tools registers only the tools listed
(manage-accounts is always there), and it refuses to start without GOOGLE_OAUTH_CREDENTIALS. Events live in a JSON
file (--store), so a test can see what was really created. No Google account, no network.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)

START = [{'id': 'e1', 'summary': 'Dentist', 'start': '2030-01-08T09:30', 'end': '2030-01-08T10:00'},
         {'id': 'e2', 'summary': 'Launch review with Dana', 'start': '2030-01-08T14:00', 'end': '2030-01-08T15:00'}]


def server(store: Path, enabled: set[str] | None) -> MCPServer:
    app = MCPServer('google-calendar')
    events = lambda: json.loads(store.read_text()) if store.exists() else list(START)  # noqa: E731

    def tool(name, annotations):
        def register(function):
            if enabled is None or name in enabled or name == 'manage-accounts':
                app.tool(name=name, annotations=annotations)(function)
            return function
        return register

    @tool('list-calendars', READ_ONLY)
    def list_calendars() -> dict:
        """List all available calendars."""
        return {'calendars': [{'id': 'primary', 'summary': 'Me'}]}

    @tool('list-events', READ_ONLY)
    def list_events(calendarId: str = 'primary', timeMin: str = '', timeMax: str = '') -> dict:  # noqa: N803
        """List events from one or more calendars."""
        return {'events': [e for e in events() if (not timeMin or e['start'] >= timeMin)
                           and (not timeMax or e['start'] < timeMax)]}

    @tool('get-event', READ_ONLY)
    def get_event(eventId: str, calendarId: str = 'primary') -> dict:  # noqa: N803
        """Get one event by its id."""
        return next((e for e in events() if e['id'] == eventId), {'error': 'not found'})

    @tool('search-events', READ_ONLY)
    def search_events(query: str, calendarId: str = 'primary') -> dict:  # noqa: N803
        """Search events by text."""
        return {'events': [e for e in events() if query.lower() in e['summary'].lower()]}

    @tool('get-freebusy', READ_ONLY)
    def get_freebusy(timeMin: str, timeMax: str) -> dict:  # noqa: N803
        """Free/busy for calendars."""
        return {'busy': [{'start': e['start'], 'end': e['end']} for e in events()]}

    @tool('get-current-time', READ_ONLY)
    def get_current_time() -> dict:
        """The current time in the calendar's timezone."""
        return {'now': '2030-01-07T09:00', 'timeZone': 'Europe/London'}

    @tool('create-event', WRITE)
    def create_event(summary: str, start: str, end: str, calendarId: str = 'primary',  # noqa: N803
                     attendees: list[str] | None = None) -> dict:
        """Create a new calendar event."""
        found = events()
        event = {'id': f'e{len(found) + 1}', 'summary': summary, 'start': start, 'end': end,
                 'attendees': attendees or []}
        store.write_text(json.dumps([*found, event]))
        return {'created': event}

    @tool('update-event', DESTRUCTIVE)
    def update_event(eventId: str, summary: str = '') -> dict:  # noqa: N803
        """Update an existing event."""
        return {'updated': eventId}

    @tool('delete-event', DESTRUCTIVE)
    def delete_event(eventId: str) -> dict:  # noqa: N803
        """Delete an event."""
        store.write_text(json.dumps([e for e in events() if e['id'] != eventId]))
        return {'deleted': eventId}

    @tool('manage-accounts', WRITE)
    def manage_accounts(action: str = 'list') -> dict:
        """Add, list or remove connected Google accounts."""
        return {'accounts': ['normal']}

    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', nargs='?', default='start')
    parser.add_argument('--enable-tools', default=None)
    parser.add_argument('--store', default=str(Path(os.environ.get('HOME', '.')) / 'calendar.json'))
    arguments = parser.parse_args()
    if not os.environ.get('GOOGLE_OAUTH_CREDENTIALS'):
        print('GOOGLE_OAUTH_CREDENTIALS is not set', file=sys.stderr)
        sys.exit(1)
    enabled = set(arguments.enable_tools.split(',')) if arguments.enable_tools else None
    server(Path(arguments.store), enabled).run()
