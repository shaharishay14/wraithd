"""
The background service: the launchd plist and the systemd unit, and install/start/stop/status/uninstall through a
fake service manager. HOME is a temp folder (conftest), so nothing lands in the real ~/Library or ~/.config.
"""

import argparse
import os
import plistlib
import sys
from pathlib import Path

import httpx
import pytest

from local_agent import __version__
from local_agent.cli import build_parser
from local_agent.daemon import commands as daemon_commands
from local_agent.daemon import service as service_module
from local_agent.daemon.service import LABEL, Launchd, ServiceError, Systemd, service_for
from tests.fixtures.services import FakeServiceManager


@pytest.fixture
def manager():
    return FakeServiceManager()


def health_transport(answer: dict | None = None, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == 'http://127.0.0.1:7878/api/health'
        if answer is None:
            raise httpx.ConnectError('connection refused')
        return httpx.Response(status, json=answer)
    return httpx.MockTransport(handler)


def args(verb, **extra):
    return argparse.Namespace(daemon_command=verb, **extra)


# --- what gets installed -----------------------------------------------------------------------------------

def test_the_launchd_agent_runs_the_daemon_at_login_and_logs_into_the_home(home, manager, tmp_path):
    service = Launchd(home, runner=manager)
    assert service.path == tmp_path / 'user' / 'Library' / 'LaunchAgents' / f'{LABEL}.plist'  # conftest's HOME
    service.install()
    plist = plistlib.loads(service.path.read_bytes())
    assert plist['Label'] == 'com.local-agent.daemon'
    assert plist['ProgramArguments'] == [sys.executable, '-m', 'local_agent', 'daemon']
    assert plist['RunAtLoad'] is True and plist['KeepAlive'] == {'SuccessfulExit': False}
    assert plist['StandardOutPath'] == plist['StandardErrorPath'] == str(home.root / 'logs' / 'daemon.log')
    environment = plist['EnvironmentVariables']
    assert environment['LOCAL_AGENT_HOME'] == str(home.root) and environment['PATH'] == os.environ['PATH']
    assert environment['PYTHONUNBUFFERED'] == '1'
    assert (home.root / 'logs').is_dir()


def test_the_systemd_unit_runs_the_daemon_quoted_and_logs_into_the_home(home, manager):
    service = Systemd(home, runner=manager, program=['/opt/my tools/python', '-m', 'local_agent', 'daemon'],
                      environment={'LOCAL_AGENT_HOME': '/home/me/100% agent', 'PATH': '/usr/bin'})
    assert service.path == Path.home() / '.config' / 'systemd' / 'user' / 'local-agent.service'
    service.install()
    unit = service.path.read_text()
    assert 'ExecStart="/opt/my tools/python" "-m" "local_agent" "daemon"\n' in unit
    assert 'Environment="LOCAL_AGENT_HOME=/home/me/100%% agent" "PATH=/usr/bin"\n' in unit  # % is a specifier
    assert 'Restart=on-failure\n' in unit and 'WantedBy=default.target\n' in unit
    log = home.root / 'logs' / 'daemon.log'
    assert f'StandardOutput=append:{log}\n' in unit and f'StandardError=append:{log}\n' in unit


def test_each_platform_gets_its_service_manager(home):
    assert isinstance(service_for(home, 'darwin'), Launchd)
    assert isinstance(service_for(home, 'linux'), Systemd)
    with pytest.raises(ServiceError, match='macOS and Linux'):
        service_for(home, 'win32')


# --- running it --------------------------------------------------------------------------------------------

def test_launchd_install_start_stop_status_uninstall(home, manager):
    service = Launchd(home, runner=manager)
    target = f'gui/{os.getuid()}/{LABEL}'
    assert service.status() == service_module.Status(installed=False, running=False)
    with pytest.raises(ServiceError, match='local-agent daemon install'):
        service.start()

    service.install()
    assert manager.commands('launchctl') == [['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(service.path)]]
    assert service.status() == service_module.Status(installed=True, running=True, pid=4242)

    service.stop()
    assert manager.commands('launchctl')[-1] == ['launchctl', 'bootout', target]
    assert service.status().running is False and service.is_installed()  # stopped, still installed
    service.start()
    assert manager.commands('launchctl')[-1][:2] == ['launchctl', 'bootstrap'] and service.status().running
    service.restart()
    assert manager.commands('launchctl')[-1] == ['launchctl', 'kickstart', '-k', target]
    service.install()  # again: the old definition is unloaded first
    assert manager.commands('launchctl')[-2:] == [['launchctl', 'bootout', target],
                                                  ['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(service.path)]]
    service.uninstall()
    assert manager.commands('launchctl')[-1] == ['launchctl', 'bootout', target] and not service.path.exists()
    assert all(call[0] == 'launchctl' for call in manager.calls)


def test_systemd_install_start_stop_status_uninstall(home, manager):
    service = Systemd(home, runner=manager)
    service.install()
    unit = 'local-agent.service'
    assert manager.commands('systemctl') == [['systemctl', '--user', 'daemon-reload'],
                                             ['systemctl', '--user', 'enable', unit],
                                             ['systemctl', '--user', 'restart', unit]]
    assert service.status() == service_module.Status(installed=True, running=True, pid=4242)
    service.stop()
    assert service.status() == service_module.Status(installed=True, running=False)
    service.start()
    assert manager.commands('systemctl')[-1] == ['systemctl', '--user', 'start', unit] and service.status().running
    service.uninstall()
    assert manager.commands('systemctl')[-2:] == [['systemctl', '--user', 'disable', '--now', unit],
                                                  ['systemctl', '--user', 'daemon-reload']]
    assert not service.path.exists() and service.status().installed is False


def test_a_refusal_from_the_service_manager_says_what_failed(home, manager):
    manager.fail['bootstrap'] = 'Bootstrap failed: 5: Input/output error'
    with pytest.raises(ServiceError, match='launchctl bootstrap failed: Bootstrap failed: 5'):
        Launchd(home, runner=manager).install()


# --- the commands ------------------------------------------------------------------------------------------

def test_daemon_alone_still_runs_in_the_foreground_and_verbs_parse():
    assert build_parser().parse_args(['daemon']).daemon_command is None
    parsed = build_parser().parse_args(['daemon', 'logs', '-n', '5', '-f'])
    assert (parsed.daemon_command, parsed.lines, parsed.follow) == ('logs', 5, True)


def test_start_installs_the_service_when_needed_and_prints_the_link(home, manager):
    service, said = Launchd(home, runner=manager), []
    assert daemon_commands.main(home, args('start'), service=service, say=said.append) == 0
    assert service.is_installed() and manager.running
    assert said[0].startswith('Installed the background service')
    token = (home.root / 'api-token').read_text().strip()
    assert said[-1] == f'Web UI: http://127.0.0.1:7878/#token={token}'
    assert (home.root / 'api-token').stat().st_mode & 0o777 == 0o600

    said.clear()
    assert daemon_commands.main(home, args('stop'), service=service, say=said.append) == 0
    assert not manager.running and said[0].startswith('Stopped')


def test_status_reports_the_service_and_whether_the_daemon_answers(home, manager):
    service, said = Launchd(home, runner=manager), []
    assert daemon_commands.main(home, args('status'), service=service, transport=health_transport(),
                                say=said.append) == 1
    assert said == ['Background service: not installed (local-agent daemon install)',
                    'Daemon: not answering on port 7878']
    service.install()
    said.clear()
    assert daemon_commands.main(home, args('status'), service=service,
                                transport=health_transport({'ok': True, 'version': __version__}),
                                say=said.append) == 0
    assert said[0] == f'Background service: launchd agent at {service.path}, running (pid 4242)'
    assert said[1] == f'Daemon: answering on port 7878 (version {__version__})'
    assert said[2].startswith('Web UI: http://127.0.0.1:7878/#token=')


def test_a_service_error_is_a_message_and_exit_code_1(home, manager, capsys):
    manager.fail['bootstrap'] = 'no GUI session'
    assert daemon_commands.main(home, args('install'), service=Launchd(home, runner=manager), say=print) == 1
    assert 'no GUI session' in capsys.readouterr().err


def test_logs_shows_the_end_of_the_log(home):
    said = []
    assert daemon_commands.main(home, args('logs', lines=2, follow=False), say=said.append) == 0
    assert said[0].startswith('No log yet')
    log = service_module.log_path(home)
    log.parent.mkdir()
    log.write_text('one\ntwo\nthree\n')
    said.clear()
    daemon_commands.main(home, args('logs', lines=2, follow=False), say=said.append)
    assert said == ['two', 'three']


def test_health_is_only_a_local_agent_answer():
    assert service_module.health(7878, health_transport({'ok': True, 'version': '1'})) == {'ok': True, 'version': '1'}
    assert service_module.health(7878, health_transport({'something': 'else'})) is None
    assert service_module.health(7878, health_transport()) is None
