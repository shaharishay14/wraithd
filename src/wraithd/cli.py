"""
The `wraithd` command.

    wraithd onboard                  set everything up, one question at a time (safe to run again)
    wraithd chat                     talk to the agent in the terminal
    wraithd runs list                the latest runs
    wraithd runs show <id> [--json]  one run's timeline (a unique prefix of the id is enough)
    wraithd events [--type T]        what happened between runs: compaction, distill, ignored Telegram chats
    wraithd schedules list [--all]   schedules and reminders, with their next times
    wraithd schedules rm <id>        cancel one
    wraithd mcp add|list|tools|allow|disallow|remove   your apps, as MCP servers (see mcp/commands.py)
    wraithd daemon                   the API, web UI, scheduler and Telegram, in the foreground
    wraithd daemon install|start|stop|restart|status|logs|uninstall   the same, as a background service
    wraithd doctor                   check the setup; a fix for anything wrong
    wraithd audit                    check the live setup against the security rules
    wraithd update                   upgrade (with uv or pipx) and restart the service
    wraithd telegram pair            a code that makes your Telegram chat the owner's
    wraithd key set <provider>       store an API key in the OS keychain
    wraithd key rm <provider>        delete it from the keychain
    wraithd --plain ...              the plain look: typed answers, no colors (also NO_COLOR, WRAITHD_PLAIN=1)

On a terminal, lists are tables and a command missing its id (schedules rm, runs show, runs tokens, key rm,
review show|approve|reject, mcp remove) offers a picker; in plain mode they're as they always were (docs/cli.md).
"""

import argparse
import asyncio
import getpass
import json
import logging
import sys

from wraithd import __version__
from wraithd.config import KEY_NAMES, ConfigError, Home, NoKeychain, delete_key, set_key
from wraithd.core import events as ev
from wraithd.core import inspector, views
from wraithd.daemon import commands as daemon_commands
from wraithd.mcp import commands as mcp_commands
from wraithd.providers import ProviderError
from wraithd.store import SQLiteStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='wraithd',
        description='A personal agent that runs on your own machine.',
    )
    parser.add_argument('--version', action='version', version=f'wraithd {__version__}')
    parser.add_argument('--plain', action='store_true',
                        help='no colors, pickers or tables: typed answers and plain lines, as in a script')
    commands = parser.add_subparsers(dest='command')

    commands.add_parser('onboard', help='set up how it thinks (Claude subscription, API key or local model), '
                                      'Telegram and the background service')
    commands.add_parser('chat', help='talk to the agent in the terminal')

    runs = commands.add_parser('runs', help='look at what runs did').add_subparsers(dest='runs_command',
                                                                                   required=True)
    listing = runs.add_parser('list', help='the latest runs')
    listing.add_argument('--limit', type=int, default=20)
    show = runs.add_parser('show', help="one run's timeline")
    show.add_argument('run_id', help='the run id, or a unique prefix of it')
    show.add_argument('--json', action='store_true', help='the summary and every event, as JSON')

    events = commands.add_parser('events', help='what happened between runs: compaction, distill, ignored chats')
    events.add_argument('--limit', type=int, default=20)
    events.add_argument('--type', choices=ev.BETWEEN_RUNS, action='append', help='only this type (repeatable)')

    schedules = commands.add_parser('schedules', help='schedules and reminders').add_subparsers(
        dest='schedules_command', required=True)
    schedules_list = schedules.add_parser('list', help='active schedules (--all: cancelled and finished too)')
    schedules_list.add_argument('--all', action='store_true')
    schedules_rm = schedules.add_parser('rm', help='cancel a schedule')
    schedules_rm.add_argument('schedule_id', type=int)  # on a terminal, a picker fills it in (fill_missing_id)

    mcp_commands.add_arguments(commands)
    daemon_commands.add_arguments(commands)
    commands.add_parser('doctor', help='check the setup: settings, keys, permissions, the service, the daemon')
    commands.add_parser('audit', help='check the setup against the security rules: the API, the token, the shell, '
                                      'file permissions')
    commands.add_parser('update', help='upgrade wraithd (uv or pipx) and restart the background service')
    telegram = commands.add_parser('telegram', help='the Telegram channel').add_subparsers(
        dest='telegram_command', required=True)
    telegram.add_parser('pair', help='a code to send to your bot as /pair <code> (valid for 10 minutes)')

    key = commands.add_parser('key', help='API keys, kept in the OS keychain').add_subparsers(dest='key_command',
                                                                                           required=True)
    key_set = key.add_parser('set', help='store a key (asked for without echo)')
    key_set.add_argument('provider', choices=KEY_NAMES)
    key_rm = key.add_parser('rm', help='delete a key from the keychain')
    key_rm.add_argument('provider', choices=KEY_NAMES)
    # New commands: each worker adds its subparser to `commands` inside its own block only
    # >>> security
    from wraithd.review import cli as review_cli

    review_cli.add_arguments(commands)
    # <<< security
    # >>> efficiency
    tokens = runs.add_parser('tokens', help="where one run's input tokens went, part by part")
    tokens.add_argument('run_id', help='the run id, or a unique prefix of it')
    tokens.add_argument('--json', action='store_true')
    tokens.set_defaults(command='runs_tokens')  # handled in main's efficiency block
    # <<< efficiency
    # >>> capabilities
    from wraithd.daemon import heartbeat

    heartbeat.add_arguments(commands)
    from wraithd.daemon import watch

    watch.add_arguments(commands)
    # <<< capabilities
    # >>> reach
    # <<< reach
    # >>> evals
    # <<< evals
    # >>> sandbox
    # <<< sandbox
    return parser


def _store(home: Home) -> SQLiteStore | None:
    return SQLiteStore(home.db_path) if home.db_path.exists() else None


def _rich():
    """A rich console on a terminal; None in plain mode (wraithd.tui.plain)."""
    from wraithd import tui

    if tui.plain():
        return None
    from wraithd.tui import theme

    return theme.console()


def run_numbers(events) -> tuple[str, int, int]:
    """(outcome, raw tokens, weighted tokens) of one run: weighted is what the budgets count (views.weighted_tokens);
    a run_end from before it was recorded counts its raw tokens."""
    summary = inspector.summarize(events)
    end = next((event for event in reversed(events) if event.type == ev.RUN_END), None)
    raw = end.payload.get('tokens', summary['tokens']) if end else summary['tokens']
    weighted = end.payload.get('weighted_tokens', raw) if end else views.weighted_tokens(events)
    return summary['outcome'], raw, weighted


def runs_list(home: Home, limit: int) -> int:
    store = _store(home)
    runs = store.recent_runs(limit) if store else []
    if not runs:
        print('No runs yet.')
        return 0
    numbers = {info.run_id: run_numbers(store.run_events(info.run_id)) for info in runs}
    if console := _rich():
        from rich.text import Text

        from wraithd.tui import tables

        out = tables.table('id:nowrap:dim', 'started:nowrap', 'source:nowrap', 'outcome:nowrap', 'tokens:nowrap',
                           'weighted:nowrap', 'message:grow')
        for info in runs:
            outcome, raw, weighted = numbers[info.run_id]
            label, style = tables.OUTCOME.get(outcome, (outcome or '', 'dim'))
            out.add_row(info.run_id[:8], f'{info.started_at.astimezone():%Y-%m-%d %H:%M}', info.source,
                        Text(label, style=style), f'{raw:,}', f'{weighted:,}', Text(info.message.replace('\n', ' ')))
        console.print(out)
        return 0
    for info in runs:
        _, raw, weighted = numbers[info.run_id]
        message = info.message if len(info.message) <= 60 else info.message[:57] + '...'
        print(f'{info.run_id[:8]}  {info.started_at.astimezone():%Y-%m-%d %H:%M}  {info.source:<9} '
              f'{raw:>9,} {weighted:>9,}  {message}')
    return 0


def runs_show(home: Home, prefix: str, as_json: bool) -> int:
    store = _store(home)
    matches = store.find_runs(prefix) if store and prefix else []
    if len(matches) != 1:
        print(f'No run matches {prefix!r}.' if not matches else
              f'{len(matches)} runs start with {prefix!r}; give more of the id.', file=sys.stderr)
        return 1
    events = store.run_events(matches[0])
    if as_json:
        print(json.dumps({'run_id': matches[0], 'summary': inspector.summarize(events),
                          'events': inspector.timeline(events)}, indent=1, default=str))
    else:
        print(inspector.render_text(matches[0], events, children=store.run_events))  # subagents nested
    return 0


def events_list(home: Home, limit: int, types) -> int:
    store = _store(home)
    listed = store.events_between_runs(limit, types or ()) if store else []
    if not listed:
        print('Nothing has happened between runs yet.')
        return 0
    if console := _rich():
        from wraithd.tui import tables

        console.print(tables.events([(event, inspector.between_runs_text(event)) for event in listed]))
        return 0
    for event in listed:
        print(f'{event.created_at.astimezone():%Y-%m-%d %H:%M}  {event.type:<16} {inspector.between_runs_text(event)}')
    return 0


def schedules_list(home: Home, show_all: bool) -> int:
    from wraithd.schedules import describe

    store = _store(home)
    listed = store.list_schedules(active_only=not show_all) if store else []
    if not listed:
        print('No schedules.')
        return 0
    if console := _rich():
        from wraithd.tui import tables

        console.print(tables.schedules([(schedule, describe(schedule)) for schedule in listed]))
        return 0
    for schedule in listed:
        state = '' if schedule.active else '  [off]'
        print(f'{schedule.id:>4}  {describe(schedule)}{state}\n      {schedule.message}')
    return 0


def schedules_rm(home: Home, schedule_id: int) -> int:
    store = _store(home)
    if not store or not store.cancel_schedule(schedule_id):
        print(f'No active schedule {schedule_id}.', file=sys.stderr)
        return 1
    print(f'Cancelled schedule {schedule_id}.')
    return 0


def checks(home: Home, module) -> int:
    """doctor or audit: their own lines in plain mode, a table on a terminal. The exit code is the same."""
    console = _rich()
    if console is None:
        return module.main(home)
    from wraithd.doctor import FAIL
    from wraithd.tui import tables

    found = module.checks(home)
    console.print(tables.checks(found))
    console.print(tables.checks_summary(found))
    return 1 if any(check.state == FAIL for check in found) else 0


# What a command left without its id can pick from: (command words, how many words, the id's choices)
def _run_choices(home: Home) -> list[tuple[str, str]]:
    store = _store(home)
    return [(info.run_id[:8], f'{info.started_at.astimezone():%m-%d %H:%M}  {info.source:<9} {info.message[:50]}')
            for info in (store.recent_runs(20) if store else [])]


def _schedule_choices(home: Home) -> list[tuple[str, str]]:
    from wraithd.schedules import describe

    store = _store(home)
    return [(str(s.id), f'{describe(s)}  {s.message[:50]}') for s in (store.list_schedules() if store else [])]


def _key_choices(home: Home) -> list[tuple[str, str]]:
    from wraithd.config import get_key

    return [(name, 'in the keychain') for name in KEY_NAMES if get_key(name)]


def _review_choices(home: Home) -> list[tuple[str, str]]:
    from wraithd.review import queue

    store = _store(home)
    return [(str(item.id), f'{item.kind:<9} {item.summary[:50]}')
            for item in (queue.ReviewQueue(store).list(queue.PENDING) if store else [])]


def _mcp_choices(home: Home) -> list[tuple[str, str]]:
    from wraithd.config import load_config

    return [(server.name, server.url or ' '.join((server.command, *server.args)))
            for server in load_config(home).mcp_servers]


NEEDS_ID = {
    ('schedules', 'rm'): ('Cancel which schedule?', _schedule_choices, 'No schedules.'),
    ('runs', 'show'): ('Which run?', _run_choices, 'No runs yet.'),
    ('runs', 'tokens'): ('Which run?', _run_choices, 'No runs yet.'),
    ('key', 'rm'): ('Delete which key?', _key_choices, 'No keys in the keychain.'),
    ('review', 'show'): ('Which item?', _review_choices, 'Nothing waits for your review.'),
    ('review', 'approve'): ('Approve which item?', _review_choices, 'Nothing waits for your review.'),
    ('review', 'reject'): ('Reject which item?', _review_choices, 'Nothing waits for your review.'),
    ('mcp', 'remove'): ('Remove which server?', _mcp_choices, 'No MCP servers.'),
}
FLAGS_WITH_VALUE = {'--reason'}


def _global_flags(argv: list[str]) -> list[str]:
    """The flags before the command: `wraithd --plain chat`."""
    at = next((i for i, word in enumerate(argv) if not word.startswith('-')), len(argv))
    return argv[:at]


def fill_missing_id(argv: list[str], locate=Home.locate, pick=None) -> list[str] | int:
    """On a terminal, `wraithd schedules rm` (no id) asks which one with a picker and goes on with the id.
    Returns the arguments to parse, or an exit code when there's nothing to pick or the owner stopped."""
    at = len(_global_flags(argv))
    if tuple(argv[at:at + 2]) not in NEEDS_ID:
        return argv
    rest, given = argv[at + 2:], False
    skip = False
    for word in rest:
        if skip:
            skip = False
        elif word in FLAGS_WITH_VALUE:
            skip = True
        elif not word.startswith('-'):
            given = True
    if given or '-h' in rest or '--help' in rest:
        return argv
    from wraithd import tui

    if tui.plain():
        return argv  # argparse says the id is missing, as it always did
    question, choices, nothing = NEEDS_ID[tuple(argv[at:at + 2])]
    found = choices(locate())
    if not found:
        print(nothing)
        return 0
    from wraithd.tui.pick import Option

    if pick is None:
        from wraithd.channels.terminal import RichTerminal

        pick = RichTerminal().pick
    try:
        value = asyncio.run(pick(question, [Option(value, hint=hint) for value, hint in found], found[0][0]))
    except KeyboardInterrupt:
        value = None
    if value is None:
        return 1
    return argv[:at + 2] + [value] + rest


def daemon(home: Home) -> int:
    from wraithd.daemon.app import Daemon
    from wraithd.run import Runtime

    # Timestamps for the service's log file. WARNING and up only: httpx logs every request URL at INFO, and the
    # Telegram ones have the bot token in them
    logging.basicConfig(level=logging.WARNING, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    try:
        rt = Runtime.open(home)
    except (ConfigError, ProviderError) as e:
        print(e, file=sys.stderr)
        return 1
    try:
        asyncio.run(_serve(Daemon(rt), rt))
    except KeyboardInterrupt:
        pass
    finally:
        rt.close()
    return 0


async def _serve(daemon, rt) -> None:
    """The daemon until it stops; then its warm Claude Code sessions close, before the event loop ends."""
    try:
        await daemon.serve()
    finally:
        await rt.close_sessions()


def telegram_pair(home: Home) -> int:
    from wraithd.channels.telegram import new_pairing

    home.ensure()
    store = SQLiteStore(home.db_path)
    try:
        pairing = new_pairing(store)
    finally:
        store.close()
    print(f'Send this to your bot within 10 minutes:  /pair {pairing["code"]}')
    return 0


def key_set(provider: str) -> int:
    value = getpass.getpass(f'{provider} API key (not shown): ').strip()
    if not value:
        print('Nothing saved.', file=sys.stderr)
        return 1
    try:
        set_key(provider, value)
    except NoKeychain as e:
        print(e, file=sys.stderr)
        return 1
    print(f'Saved the {provider} key in the keychain.')
    return 0


def key_rm(provider: str) -> int:
    if not delete_key(provider):
        print(f'No {provider} key in the keychain.')
        return 0
    print(f'Deleted the {provider} key from the keychain.')
    return 0


def chat(home: Home) -> int:
    from wraithd.channels.terminal import chat as chat_session
    from wraithd.run import Runtime

    try:
        rt = Runtime.open(home)
    except (ConfigError, ProviderError) as e:
        print(e, file=sys.stderr)
        return 1
    from wraithd.channels.terminal import terminal_for

    try:
        asyncio.run(chat_session(rt, terminal_for()))
    except KeyboardInterrupt:
        pass
    finally:
        rt.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argv, server_command = mcp_commands.split_command(sys.argv[1:] if argv is None else list(argv))
    if '--plain' in _global_flags(argv):
        from wraithd import tui

        tui.set_plain()
    filled = fill_missing_id(argv, Home.locate)
    if isinstance(filled, int):
        return filled
    args = parser.parse_args(filled)
    if server_command:
        args.server_command = server_command
    home = Home.locate()
    if args.command == 'onboard':
        from wraithd import onboard
        return onboard.main(home)
    if args.command == 'doctor':
        from wraithd import doctor
        return checks(home, doctor)
    if args.command == 'audit':
        from wraithd import audit
        return checks(home, audit)
    if args.command == 'update':
        from wraithd import update
        return update.update(home)
    if args.command == 'chat':
        return chat(home)
    if args.command == 'runs':
        if args.runs_command == 'list':
            return runs_list(home, args.limit)
        return runs_show(home, args.run_id, args.json)
    if args.command == 'events':
        return events_list(home, args.limit, args.type)
    if args.command == 'schedules':
        if args.schedules_command == 'list':
            return schedules_list(home, args.all)
        return schedules_rm(home, args.schedule_id)
    if args.command == 'daemon':
        return daemon(home) if args.daemon_command is None else daemon_commands.main(home, args)
    if args.command == 'telegram':
        return telegram_pair(home)
    if args.command == 'mcp':
        return mcp_commands.main(home, args)
    if args.command == 'key':
        return key_set(args.provider) if args.key_command == 'set' else key_rm(args.provider)
    # New commands: each worker dispatches its own inside its block only
    # >>> security
    if args.command == 'review':
        from wraithd.review import cli as review_cli

        return review_cli.main(home, args)
    # <<< security
    # >>> efficiency
    if args.command == 'runs_tokens':
        from wraithd.core import tokens
        return tokens.main(home, args.run_id, args.json)
    # <<< efficiency
    # >>> capabilities
    if args.command == 'heartbeat':
        from wraithd.daemon import heartbeat

        return heartbeat.main(home, args)
    if args.command == 'watch':
        from wraithd.daemon import watch

        return watch.main(home, args)
    # <<< capabilities
    # >>> reach
    # <<< reach
    # >>> evals
    # <<< evals
    # >>> sandbox
    # <<< sandbox
    parser.print_help()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
