"""
`local-agent doctor` (every check passing, then each one failing with its fix) and `local-agent update` (finding
the installer, upgrading through a fake runner, restarting the service).
"""

import httpx
import pytest

from local_agent import __version__, doctor, update
from local_agent.config import set_key
from local_agent.daemon import app as app_module
from local_agent.daemon.security import load_or_create_token
from local_agent.daemon.service import Launchd, Systemd
from tests.fixtures.services import FakeServiceManager

OPENAI_KEY, SERP_KEY = 'sk-test-doctor-0123456789', 'serp-test-0123456789'


def answering(version=__version__):
    return httpx.MockTransport(lambda request: httpx.Response(200, json={'ok': True, 'version': version}))


def silent():
    def refuse(request):
        raise httpx.ConnectError('connection refused')
    return httpx.MockTransport(refuse)


@pytest.fixture
def manager():
    return FakeServiceManager()


@pytest.fixture
def healthy(home, manager, tmp_path, monkeypatch):
    """Everything in place: keys, a private token file, a built UI, the service installed and running."""
    set_key('openai', OPENAI_KEY)
    set_key('serpapi', SERP_KEY)
    load_or_create_token(home.root)
    ui = tmp_path / 'ui'
    ui.mkdir()
    (ui / 'index.html').write_text('<div id="app"></div>')
    monkeypatch.setattr(app_module, 'PACKAGE_UI', ui)
    service = Launchd(home, runner=manager)
    service.install()
    return service


def states(found):
    return {check.text: check.state for check in found}


def test_doctor_all_good(home, healthy, capsys):
    found = doctor.checks(home, service=healthy, transport=answering(), port_free=lambda port: True)
    assert {check.state for check in found} <= {doctor.OK, doctor.INFO}
    text = doctor.render(found)
    assert 'openai key: set' in text and 'serpapi key: set' in text
    assert 'is private (mode 700)' in text and 'API token file is private (mode 600)' in text
    assert 'Background service: running (pid 4242)' in text and f'Daemon answers on port 7878 (version {__version__})' \
        in text
    assert text.endswith('All good.')
    assert OPENAI_KEY not in text and SERP_KEY not in text  # present or missing, never the value
    assert doctor.main(home, service=healthy, transport=answering(), port_free=lambda port: True) == 0
    capsys.readouterr()


def test_doctor_names_each_problem_with_its_fix(home, manager):
    home.root.chmod(0o755)
    (home.root / 'api-token').write_text('x' * 40)
    (home.root / 'api-token').chmod(0o644)
    service = Launchd(home, runner=manager)  # not installed
    found = doctor.checks(home, service=service, transport=silent(), port_free=lambda port: False)
    fixes = {check.text: check.fix for check in found if check.state == doctor.FAIL}
    assert fixes == {
        f'{home.root} is private (mode 755)': f'chmod 700 {home.root}',
        'openai key: missing': 'local-agent key set openai',
        'API token file is private (mode 644)': f'chmod 600 {home.root / "api-token"}',
        'Web UI: not in this install (the daemon shows a placeholder)':
            'install a release build (scripts/release.sh builds one), or set LOCAL_AGENT_UI_DIR to a built '
            'frontend/dist',
        'Background service: not installed': 'local-agent daemon install',
        'Daemon: nothing answers on port 7878': 'local-agent daemon start (or run local-agent daemon)',
        'Port 7878: in use by another program':
            f'stop that program, or set another port in {home.config_path} ([daemon] port)',
        'Security rules: 2 fail': 'local-agent audit, for each rule and its fix',  # the folder and the token
    }
    assert states(found)['serpapi key: missing, so web search is off'] == doctor.INFO  # optional
    text = doctor.render(found)
    assert '        fix: local-agent key set openai' in text and text.endswith('8 problems found.')


def test_doctor_a_stopped_service_an_old_daemon_and_a_broken_config(home, healthy, manager):
    manager.running = False
    found = doctor.checks(home, service=healthy, transport=answering('0.0.0'), port_free=lambda port: True)
    fixes = {check.text: check.fix for check in found if check.state == doctor.FAIL}
    assert fixes['Background service: not running'].startswith('local-agent daemon start')
    assert fixes['Daemon answers on port 7878 (version 0.0.0)'] == \
        'it is still running the old version: local-agent daemon restart'

    home.config_path.write_text('[model]\nprovider = "gemini"\n')
    found = doctor.checks(home, service=healthy, transport=answering(), port_free=lambda port: True)
    [broken] = [check for check in found if check.text.startswith('Settings don\'t load')]
    assert 'provider must be one of' in broken.text and str(home.config_path) in broken.fix


def test_doctor_without_a_home_says_to_onboard(tmp_path):
    from local_agent.config import Home

    [python, package, missing] = doctor.checks(Home(tmp_path / 'nowhere'))
    assert missing == doctor.Check(doctor.FAIL, f'No state folder at {tmp_path / "nowhere"}', 'local-agent onboard')


def test_doctor_ollama_needs_no_key(home, manager):
    home.config_path.write_text('[model]\nprovider = "ollama"\ndefault = "llama3.2"\n')
    found = doctor.checks(home, service=Launchd(home, runner=manager), transport=silent(),
                          port_free=lambda port: True)
    assert states(found)['ollama: no key needed'] == doctor.OK
    assert states(found)['Port 7878: free'] == doctor.OK


# --- update ------------------------------------------------------------------------------------------------

def test_the_installer_is_found_from_the_environment(tmp_path):
    uv_env, pipx_env, plain = tmp_path / 'uv', tmp_path / 'pipx', tmp_path / 'plain'
    for folder in (uv_env, pipx_env, plain):
        folder.mkdir()
    (uv_env / 'uv-receipt.toml').write_text('[tool]\n')
    (pipx_env / 'pipx_metadata.json').write_text('{}')
    assert [update.installer(p) for p in (uv_env, pipx_env, plain)] == ['uv', 'pipx', None]


def test_update_with_uv_upgrades_then_restarts_the_service(home, manager, tmp_path):
    (tmp_path / 'uv-receipt.toml').write_text('[tool]\n')
    service, said = Launchd(home, runner=manager), []
    service.install()
    assert update.update(home, runner=manager, prefix=tmp_path, which=lambda name: f'/usr/bin/{name}',
                         service=service, say=said.append) == 0
    assert ['uv', 'tool', 'upgrade', 'local-agent'] in manager.calls
    assert manager.commands('launchctl')[-1][:3] == ['launchctl', 'kickstart', '-k']
    assert said[-1] == 'Restarted the background service on the new version.'


def test_update_with_pipx_and_no_service_only_upgrades(home, manager, tmp_path):
    (tmp_path / 'pipx_metadata.json').write_text('{}')
    service = Systemd(home, runner=manager)
    assert update.update(home, runner=manager, prefix=tmp_path, which=lambda name: name, service=service,
                         say=lambda text: None) == 0
    assert manager.calls == [['pipx', 'upgrade', 'local-agent']]


def test_a_failed_update_restarts_nothing(home, manager, tmp_path):
    (tmp_path / 'uv-receipt.toml').write_text('[tool]\n')
    manager.upgrade_result = (2, '', 'error: no network')
    service, said = Launchd(home, runner=manager), []
    service.install()
    before = list(manager.calls)
    assert update.update(home, runner=manager, prefix=tmp_path, which=lambda name: name, service=service,
                         say=said.append) == 1
    assert manager.calls == before + [['uv', 'tool', 'upgrade', 'local-agent']]
    assert said[-2:] == ['error: no network', 'The update failed; nothing was restarted.']


def test_update_leaves_other_installs_and_missing_tools_to_the_owner(home, manager, tmp_path):
    said = []
    assert update.update(home, runner=manager, prefix=tmp_path, say=said.append) == 1
    assert 'wasn\'t installed with uv or pipx' in said[0]
    (tmp_path / 'uv-receipt.toml').write_text('[tool]\n')
    assert update.update(home, runner=manager, prefix=tmp_path, which=lambda name: None, say=said.append) == 1
    assert said[-1] == 'Installed with uv, but `uv` isn\'t on the PATH. Run: uv tool upgrade local-agent'
    assert manager.calls == []
