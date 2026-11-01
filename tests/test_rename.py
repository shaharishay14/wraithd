"""
The move from Local Agent to wraithd (rename.py): the state folder is moved once and never merged, keys and MCP values
are still read under the old names, and the old background service is stopped and removed on install, before the
folder moves. HOME, WRAITHD_HOME and the keychain are temp ones (conftest), so the real ~/.local-agent, ~/.wraithd
and keychain are never touched.
"""

import argparse
import plistlib

import keyring
import pytest

from tests.fixtures.services import FakeServiceManager
from wraithd import config, doctor, rename
from wraithd.config import HOME_ENV, Home, get_key, set_key
from wraithd.core import secrets
from wraithd.daemon import commands as daemon_commands
from wraithd.daemon.service import Launchd, Systemd
from wraithd.mcp.config import McpServer, delete_secrets, get_secrets
from wraithd.tools.shell import _environment as shell_environment


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(rename, '_said', set())


@pytest.fixture
def old(tmp_path):
    """An old install: ~/.local-agent with a config and a memory note."""
    folder = tmp_path / 'user' / '.local-agent'
    (folder / 'workspace' / 'memory').mkdir(parents=True)
    (folder / 'config.toml').write_text('[model]\nprovider = "ollama"\n')
    (folder / 'workspace' / 'memory' / 'note.md').write_text('remember me')
    return folder


# --- the state folder ---------------------------------------------------------------------------------------

def test_the_old_folder_moves_to_the_default_place_once(old, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(HOME_ENV)
    home = Home.locate()
    assert home.root == tmp_path / 'user' / '.wraithd'
    assert not old.exists() and (home.memory_dir / 'note.md').read_text() == 'remember me'
    assert config.load_config(home).model.provider == 'ollama'
    err = capsys.readouterr().err
    assert err.count('\n') == 1 and f'moved your state from {old} to {home.root}' in err
    assert Home.locate().root == home.root and capsys.readouterr().err == ''  # nothing more to move or say


def test_the_old_folder_moves_to_wraithd_home(old, tmp_path, capsys):
    home = Home.locate()  # conftest sets WRAITHD_HOME to tmp_path/home
    assert home.root == tmp_path / 'home' and (home.root / 'config.toml').exists() and not old.exists()
    home.ensure()  # and the layout is filled in around what was moved, keeping it
    assert (home.memory_dir / 'note.md').read_text() == 'remember me'
    assert 'moved your state' in capsys.readouterr().err


def test_two_folders_are_never_merged(home, old, capsys):  # home first: the new folder exists before the old one
    assert Home.locate().root == home.root
    assert (old / 'config.toml').exists() and not (home.memory_dir / 'note.md').exists()
    assert 'both' in capsys.readouterr().err
    assert rename.both_homes(home.root)
    found = doctor.checks(home, service=Launchd(home, runner=FakeServiceManager()), port_free=lambda port: True)
    [check] = [check for check in found if 'old Local Agent folder' in check.text]
    assert check.state == doctor.FAIL and str(old) in check.text


def test_the_old_variable_still_works_while_the_new_one_is_unset(old, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(HOME_ENV)
    monkeypatch.setenv(rename.OLD_HOME_ENV, str(tmp_path / 'elsewhere'))
    assert Home.locate().root == tmp_path / 'elsewhere'
    assert old.exists() and capsys.readouterr().err == ''


def test_an_old_variable_pointing_elsewhere_leaves_the_old_default_folder_alone(old, tmp_path, monkeypatch):
    monkeypatch.setenv(rename.OLD_HOME_ENV, str(tmp_path / 'elsewhere'))
    assert Home.locate().root == tmp_path / 'home'  # WRAITHD_HOME wins
    assert old.exists() and not (tmp_path / 'home').exists()


def test_a_failed_move_keeps_using_the_old_folder(old, monkeypatch, capsys):
    def refuse(source, target):
        raise OSError('cross-device link')

    monkeypatch.setattr(rename.os, 'replace', refuse)
    assert Home.locate().root == old
    assert 'could not move' in capsys.readouterr().err


def test_no_old_folder_changes_nothing(tmp_path, capsys):
    assert Home.locate().root == tmp_path / 'home' and capsys.readouterr().err == ''


# --- keys and MCP values -----------------------------------------------------------------------------------

def test_keys_are_read_new_then_old_then_environment_and_written_new_only(isolated, monkeypatch):
    keyring.set_password(rename.OLD_APP, 'openai', 'sk-old-key-0123456789abcdefghij')
    assert get_key('openai') == 'sk-old-key-0123456789abcdefghij'
    set_key('openai', 'sk-new-key-0123456789abcdefghij')
    assert isolated.passwords[(config.APP, 'openai')] == 'sk-new-key-0123456789abcdefghij'
    assert isolated.passwords[(rename.OLD_APP, 'openai')] == 'sk-old-key-0123456789abcdefghij'  # untouched
    assert get_key('openai') == 'sk-new-key-0123456789abcdefghij'
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-ant-from-the-environment-0123456789')
    assert get_key('anthropic') == 'sk-ant-from-the-environment-0123456789'
    keyring.set_password(rename.OLD_APP, 'anthropic', 'sk-ant-old-keychain-0123456789abcd')
    assert get_key('anthropic') == 'sk-ant-old-keychain-0123456789abcd'  # the keychain before the environment


def test_mcp_values_are_read_under_the_old_names(isolated, monkeypatch):
    server = McpServer(name='x', transport='stdio', command='x', env=('API_KEY',), headers=('TOKEN',))
    keyring.set_password(rename.OLD_APP, 'mcp/x/env/API_KEY', 'old-keychain-value')
    monkeypatch.setenv('LOCAL_AGENT_MCP_X_TOKEN', 'old-environment-value')
    assert get_secrets(server) == ({'API_KEY': 'old-keychain-value'}, {'TOKEN': 'old-environment-value'})
    monkeypatch.setenv('WRAITHD_MCP_X_TOKEN', 'new-environment-value')
    assert get_secrets(server)[1] == {'TOKEN': 'new-environment-value'}
    delete_secrets(server)
    assert (rename.OLD_APP, 'mcp/x/env/API_KEY') not in isolated.passwords


def test_the_shell_never_sees_either_prefix(monkeypatch):
    monkeypatch.setenv('LOCAL_AGENT_MCP_X_TOKEN', 'old-environment-value')
    monkeypatch.setenv('WRAITHD_MCP_X_TOKEN', 'new-environment-value')
    assert secrets.is_secret_variable('LOCAL_AGENT_MCP_X_TOKEN') and secrets.is_secret_variable('WRAITHD_MCP_X_TOKEN')
    environment = shell_environment()
    assert 'LOCAL_AGENT_MCP_X_TOKEN' not in environment and 'WRAITHD_MCP_X_TOKEN' not in environment


# --- the background service --------------------------------------------------------------------------------

@pytest.mark.parametrize('kind, service_class', [('launchd', Launchd), ('systemd', Systemd)])
def test_install_removes_the_old_service_and_doctor_names_a_leftover(home, tmp_path, kind, service_class, capsys):
    manager = FakeServiceManager()
    service = service_class(home, runner=manager)
    leftover = rename.old_service_file(service.user_home, kind)
    leftover.parent.mkdir(parents=True)
    leftover.write_text('old')
    found = doctor.checks(home, service=service, port_free=lambda port: True)
    [check] = [check for check in found if 'old Local Agent service' in check.text]
    assert check.state == doctor.FAIL and check.fix == 'wraithd daemon install (removes it)'

    service.install()
    assert not leftover.exists() and service.path.is_file()
    if kind == 'launchd':
        assert ['launchctl', 'bootout', f'gui/{rename.os.getuid()}/{rename.OLD_LABEL}'] in manager.calls
    else:
        assert ['systemctl', '--user', 'disable', '--now', rename.OLD_UNIT] in manager.calls
    assert 'removed the old Local Agent service' in capsys.readouterr().err
    found = doctor.checks(home, service=service, port_free=lambda port: True)
    assert not [check for check in found if 'old Local Agent service' in check.text]


# --- the order: the old service lets go of the folder before it moves -----------------------------------------

def put_old_service(kind: str, tmp_path):
    path = rename.old_service_file(tmp_path / 'user', kind)
    path.parent.mkdir(parents=True)
    path.write_text('old')
    return path


@pytest.mark.parametrize('kind', ['launchd', 'systemd'])
def test_the_folder_waits_while_the_old_service_is_installed(old, tmp_path, monkeypatch, capsys, kind):
    monkeypatch.delenv(HOME_ENV)
    monkeypatch.setattr(rename, 'service_kind', lambda: kind)
    put_old_service(kind, tmp_path)
    assert Home.locate().root == old  # its daemon may have the store open: not moved under it
    assert old.exists() and not (tmp_path / 'user' / '.wraithd').exists()
    err = capsys.readouterr().err
    assert err == ('wraithd: the old Local Agent service is still installed; run `wraithd daemon install` to finish '
                   f'the move to {tmp_path / "user" / ".wraithd"}.\n')


@pytest.mark.parametrize('kind, service_class', [('launchd', Launchd), ('systemd', Systemd)])
def test_install_stops_the_old_service_then_moves_then_installs_on_the_new_path(
        old, tmp_path, monkeypatch, kind, service_class):
    monkeypatch.delenv(HOME_ENV)
    monkeypatch.setattr(rename, 'service_kind', lambda: kind)
    leftover = put_old_service(kind, tmp_path)
    new = tmp_path / 'user' / '.wraithd'
    manager = FakeServiceManager()
    seen = []

    def runner(argv):  # what the folders looked like when each command ran
        seen.append((argv, old.exists(), new.exists()))
        return manager(argv)

    service = service_class(Home.locate(), runner=runner)
    assert service.home.root == old
    said = []
    assert daemon_commands.main(service.home, argparse.Namespace(daemon_command='install'), service=service,
                                say=said.append) == 0
    stop = next(entry for entry in seen if rename.OLD_LABEL in ' '.join(entry[0]) or rename.OLD_UNIT in entry[0])
    assert stop[1:] == (True, False)  # the old daemon was stopped while the folder was still where it expected
    assert not leftover.exists() and not old.exists()
    assert service.home.root == new and (new / 'workspace' / 'memory' / 'note.md').read_text() == 'remember me'
    if kind == 'launchd':
        plist = plistlib.loads(service.path.read_bytes())
        assert plist['EnvironmentVariables']['WRAITHD_HOME'] == str(new)
        assert plist['StandardOutPath'] == str(new / 'logs' / 'daemon.log')
    else:
        unit = service.path.read_text()
        assert f'"WRAITHD_HOME={new}"' in unit and f'StandardOutput=append:{new / "logs" / "daemon.log"}' in unit
    assert not old.exists()  # the web link that install prints didn't make a new old folder
    assert (new / 'api-token').exists() and any('#token=' in line for line in said)
