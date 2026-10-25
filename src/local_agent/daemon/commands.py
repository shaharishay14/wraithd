"""
`local-agent daemon ...`: the daemon in the foreground, or as a background service.

    local-agent daemon               run it here, in the foreground (what the service runs)
    local-agent daemon install       install the service (starts at login, restarts after a crash) and start it
    local-agent daemon start|stop|restart
    local-agent daemon status        installed? running? answering? and the web UI link
    local-agent daemon logs [-n N] [-f]
    local-agent daemon uninstall     stop it and remove the service

`start` installs the service first if it isn't installed yet, so one command is enough.
"""

import sys
import time

from local_agent.config import ConfigError, Home, load_config
from local_agent.daemon import service as service_module
from local_agent.daemon.service import ServiceError

VERBS = ('install', 'uninstall', 'start', 'stop', 'restart', 'status', 'logs')


def add_arguments(subparsers) -> None:
    daemon = subparsers.add_parser('daemon', help='the API, web UI, scheduler and Telegram: in the foreground, or '
                                                  'as a background service (install, start, stop, status, logs)')
    verbs = daemon.add_subparsers(dest='daemon_command')  # none: run in the foreground
    verbs.add_parser('install', help='install the background service (starts at login) and start it')
    verbs.add_parser('uninstall', help='stop the background service and remove it')
    verbs.add_parser('start', help='start the background service (installing it first if needed)')
    verbs.add_parser('stop', help='stop the background service (it starts again at the next login)')
    verbs.add_parser('restart', help='restart the background service')
    verbs.add_parser('status', help='whether the service is installed and running, and the web UI link')
    logs = verbs.add_parser('logs', help='the daemon\'s log')
    logs.add_argument('-n', '--lines', type=int, default=50, help='how many lines from the end (default 50)')
    logs.add_argument('-f', '--follow', action='store_true', help='keep printing new lines (Ctrl-C to stop)')


def port(home: Home) -> int:
    try:
        return load_config(home).daemon_port
    except ConfigError:
        return 7878


def web_link(home: Home) -> str:
    """The web UI's address with the token in the fragment (the token is created now if the daemon hasn't yet)."""
    from local_agent.daemon.security import load_or_create_token

    home.ensure()
    return f'http://127.0.0.1:{port(home)}/#token={load_or_create_token(home.root)}'


def status(home: Home, service, transport=None, say=print) -> int:
    state = service.status()
    if not state.installed:
        say('Background service: not installed (local-agent daemon install)')
    else:
        say(f'Background service: {service.name} at {service.path}, '
            + (f'running (pid {state.pid})' if state.running and state.pid else
               'running' if state.running else 'not running (local-agent daemon start)'))
    answer = service_module.health(port(home), transport)
    if answer:
        say(f'Daemon: answering on port {port(home)} (version {answer.get("version", "?")})')
        say(f'Web UI: {web_link(home)}')
    else:
        say(f'Daemon: not answering on port {port(home)}' + (' (local-agent daemon logs)' if state.installed else ''))
    return 0 if answer else 1


def logs(home: Home, lines: int, follow: bool, say=print, sleep=time.sleep) -> int:
    path = service_module.log_path(home)
    if not path.exists():
        say(f'No log yet ({path}). The background service writes it once it runs.')
        return 0
    with path.open(errors='replace') as log:
        for line in log.readlines()[-lines:] if lines > 0 else []:
            say(line.rstrip('\n'))
        while follow:
            line = log.readline()
            if line:
                say(line.rstrip('\n'))
            else:
                sleep(0.5)
    return 0


def main(home: Home, args, service=None, transport=None, say=print) -> int:
    """Everything but the foreground run (cli.daemon does that). `service` and `transport` are for tests."""
    verb = args.daemon_command
    if verb == 'logs':
        try:
            return logs(home, args.lines, args.follow, say)
        except KeyboardInterrupt:
            return 0
    try:
        service = service or service_module.service_for(home)
        if verb == 'status':
            return status(home, service, transport, say)
        if verb == 'install' or (verb == 'start' and not service.is_installed()):
            service.install()
            say(f'Installed the background service ({service.path}). It starts at login and is starting now.')
            say(f'Web UI: {web_link(home)}')
        elif verb == 'uninstall':
            service.uninstall()
            say('Stopped and removed the background service.')
        elif verb == 'start':
            service.start()
            say('Started.')
            say(f'Web UI: {web_link(home)}')
        elif verb == 'stop':
            service.stop()
            say('Stopped. It starts again at the next login (local-agent daemon uninstall to remove it).')
        elif verb == 'restart':
            service.restart()
            say('Restarted.')
    except ServiceError as e:
        print(e, file=sys.stderr)
        return 1
    return 0
