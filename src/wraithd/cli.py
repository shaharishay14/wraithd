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
"""

import argparse
import asyncio
import getpass
import json
import logging
import sys

from wraithd import __version__
from wraithd.config import KEY_NAMES, ConfigError, Home, NoKeychain, set_key
from wraithd.core import events as ev
from wraithd.core import inspector
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
    commands = parser.add_subparsers(dest='command')

    commands.add_parser('onboard', help='set up the model, keys, Telegram and the background service')
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
    schedules_rm.add_argument('schedule_id', type=int)

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


def runs_list(home: Home, limit: int) -> int:
    store = _store(home)
    runs = store.recent_runs(limit) if store else []
    if not runs:
        print('No runs yet.')
        return 0
    for info in runs:
        message = info.message if len(info.message) <= 60 else info.message[:57] + '...'
        print(f'{info.run_id[:8]}  {info.started_at.astimezone():%Y-%m-%d %H:%M}  {info.source:<9} {message}')
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
        asyncio.run(Daemon(rt).serve())
    except KeyboardInterrupt:
        pass
    finally:
        rt.close()
    return 0


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


def chat(home: Home) -> int:
    from wraithd.channels.terminal import chat as chat_session
    from wraithd.run import Runtime

    try:
        rt = Runtime.open(home)
    except (ConfigError, ProviderError) as e:
        print(e, file=sys.stderr)
        return 1
    try:
        asyncio.run(chat_session(rt))
    except KeyboardInterrupt:
        pass
    finally:
        rt.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argv, server_command = mcp_commands.split_command(sys.argv[1:] if argv is None else list(argv))
    args = parser.parse_args(argv)
    if server_command:
        args.server_command = server_command
    home = Home.locate()
    if args.command == 'onboard':
        from wraithd import onboard
        return onboard.main(home)
    if args.command == 'doctor':
        from wraithd import doctor
        return doctor.main(home)
    if args.command == 'audit':
        from wraithd import audit
        return audit.main(home)
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
        return key_set(args.provider)
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
