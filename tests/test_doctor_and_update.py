"""
`wraithd doctor` (every check passing, then each one failing with its fix) and `wraithd update` (finding
the installer, upgrading through a fake runner, restarting the service).
"""

import httpx
import pytest

from tests.fixtures.services import FakeServiceManager
from wraithd import __version__, doctor, update
from wraithd.config import set_key
from wraithd.daemon import app as app_module
from wraithd.daemon.security import load_or_create_token
from wraithd.daemon.service import Launchd, Systemd

OPENAI_KEY = 'sk-test-doctor-0123456789'


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
    assert 'openai key: set' in text
    # web search needs no key of its own: OpenAI's, on the model key
    assert 'Web search: openai (your model key), about $10 per 1,000 searches on your OpenAI account' in text
    assert 'Voice notes: openai (your OpenAI key is set), about $0.003 per minute on your OpenAI account' in text
    assert 'is private (mode 700)' in text and 'API token file is private (mode 600)' in text
    assert 'Background service: running (pid 4242)' in text and f'Daemon answers on port 7878 (version {__version__})' \
        in text
    assert text.endswith('All good.')
    assert OPENAI_KEY not in text  # present or missing, never the value
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
        'openai key: missing': 'wraithd key set openai',
        'API token file is private (mode 644)': f'chmod 600 {home.root / "api-token"}',
        'Web UI: not in this install (the daemon shows a placeholder)':
            'install a release build (scripts/release.sh builds one), or set WRAITHD_UI_DIR to a built '
            'frontend/dist',
        'Background service: not installed': 'wraithd daemon install',
        'Daemon: nothing answers on port 7878': 'wraithd daemon start (or run wraithd daemon)',
        'Port 7878: in use by another program':
            f'stop that program, or set another port in {home.config_path} ([daemon] port)',
        'Security rules: 2 fail': 'wraithd audit, for each rule and its fix',  # the folder and the token
    }
    assert states(found)['Web search: off: openai search needs your model key, which is missing '
                         '(wraithd key set openai)'] == doctor.INFO  # the model key's own check fails
    text = doctor.render(found)
    assert '        fix: wraithd key set openai' in text and text.endswith('8 problems found.')


def test_doctor_a_stopped_service_an_old_daemon_and_a_broken_config(home, healthy, manager):
    manager.running = False
    found = doctor.checks(home, service=healthy, transport=answering('0.0.0'), port_free=lambda port: True)
    fixes = {check.text: check.fix for check in found if check.state == doctor.FAIL}
    assert fixes['Background service: not running'].startswith('wraithd daemon start')
    assert fixes['Daemon answers on port 7878 (version 0.0.0)'] == \
        'it is still running the old version: wraithd daemon restart'

    home.config_path.write_text('[model]\nprovider = "gemini"\n')
    found = doctor.checks(home, service=healthy, transport=answering(), port_free=lambda port: True)
    [broken] = [check for check in found if check.text.startswith('Settings don\'t load')]
    assert 'provider must be one of' in broken.text and str(home.config_path) in broken.fix


def test_doctor_without_a_home_says_to_onboard(tmp_path):
    from wraithd.config import Home

    [python, package, missing] = doctor.checks(Home(tmp_path / 'nowhere'))
    assert missing == doctor.Check(doctor.FAIL, f'No state folder at {tmp_path / "nowhere"}', 'wraithd onboard')


def test_doctor_ollama_needs_no_key(home, manager):
    home.config_path.write_text('[model]\nprovider = "ollama"\ndefault = "llama3.2"\n')
    found = doctor.checks(home, service=Launchd(home, runner=manager), transport=silent(),
                          port_free=lambda port: True)
    assert states(found)['ollama: no key needed'] == doctor.OK
    assert states(found)['Port 7878: free'] == doctor.OK
    assert states(found)['Web search: duckduckgo (free, no key, since a local model has no provider search)'] == \
        doctor.OK


@pytest.mark.parametrize('web, text, state', [
    ('search = "serpapi"', 'Web search: off: serpapi search needs your serpapi key, which is missing '
                           '(wraithd key set serpapi)', doctor.INFO),
    ('search = ""', 'Web search: off in the settings ([web] search = "")', doctor.INFO),
    ('search = "anthropic"', 'Web search: anthropic (your anthropic key), about $10 per 1,000 searches on your '
                             'Anthropic account', doctor.OK),
])
def test_doctor_says_which_search_runs_and_why(home, manager, web, text, state):
    set_key('openai', OPENAI_KEY)
    set_key('anthropic', 'sk-ant-test-doctor-0123456789')
    home.config_path.write_text(f'[web]\n{web}\n')
    found = doctor.checks(home, service=Launchd(home, runner=manager), transport=silent(),
                          port_free=lambda port: True)
    assert states(found)[text] == state


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
    assert ['uv', 'tool', 'upgrade', 'wraithd'] in manager.calls
    assert manager.commands('launchctl')[-1][:3] == ['launchctl', 'kickstart', '-k']
    assert said[-1] == 'Restarted the background service on the new version.'


def test_update_with_pipx_and_no_service_only_upgrades(home, manager, tmp_path):
    (tmp_path / 'pipx_metadata.json').write_text('{}')
    service = Systemd(home, runner=manager)
    assert update.update(home, runner=manager, prefix=tmp_path, which=lambda name: name, service=service,
                         say=lambda text: None) == 0
    assert manager.calls == [['pipx', 'upgrade', 'wraithd']]


def test_a_failed_update_restarts_nothing(home, manager, tmp_path):
    (tmp_path / 'uv-receipt.toml').write_text('[tool]\n')
    manager.upgrade_result = (2, '', 'error: no network')
    service, said = Launchd(home, runner=manager), []
    service.install()
    before = list(manager.calls)
    assert update.update(home, runner=manager, prefix=tmp_path, which=lambda name: name, service=service,
                         say=said.append) == 1
    assert manager.calls == before + [['uv', 'tool', 'upgrade', 'wraithd']]
    assert said[-2:] == ['error: no network', 'The update failed; nothing was restarted.']


def test_update_leaves_other_installs_and_missing_tools_to_the_owner(home, manager, tmp_path):
    said = []
    assert update.update(home, runner=manager, prefix=tmp_path, say=said.append) == 1
    assert 'wasn\'t installed with uv or pipx' in said[0]
    (tmp_path / 'uv-receipt.toml').write_text('[tool]\n')
    assert update.update(home, runner=manager, prefix=tmp_path, which=lambda name: None, say=said.append) == 1
    assert said[-1] == 'Installed with uv, but `uv` isn\'t on the PATH. Run: uv tool upgrade wraithd'
    assert manager.calls == []


def test_doctor_says_how_voice_notes_are_transcribed_or_how_to_turn_them_on(home, manager, monkeypatch):
    from wraithd import voice

    monkeypatch.setattr(voice, 'whisper_installed', lambda: False)
    off = states(doctor.checks(home, service=Launchd(home, runner=manager), transport=silent()))
    [line] = [text for text in off if text.startswith('Voice notes:')]
    assert 'off: no OpenAI key' in line and 'wraithd[whisper]' in line and off[line] == doctor.INFO
    monkeypatch.setattr(voice, 'whisper_installed', lambda: True)
    found = states(doctor.checks(home, service=Launchd(home, runner=manager), transport=silent()))
    assert found['Voice notes: whisper (installed, no OpenAI key), free, runs on this computer'] == doctor.OK


def test_doctor_in_subscription_mode_names_the_search_key_and_the_fallback_models(home, healthy):
    """On claude-code, [model] is only what a run falls back to, and 'your model key' would be unclear."""
    from wraithd.config import set_values

    set_values(home, 'engine', {'name': 'claude-code'})

    async def signed_in(home):
        return True, 'Claude Code uses your Claude login'

    text = doctor.render(doctor.checks(home, service=healthy, transport=answering(), port_free=lambda port: True,
                                       login_check=signed_in))
    assert 'Web search: openai (your OpenAI key), about $10 per 1,000 searches on your OpenAI account' in text
    assert 'your model key' not in text
    model_lines = [line.strip() for line in text.splitlines() if 'gpt-4o' in line]
    assert model_lines and all(line.split(maxsplit=1)[1].startswith('Fallback model') for line in model_lines)


def test_doctor_on_the_api_engine_keeps_the_model_labels(home, healthy):
    text = doctor.render(doctor.checks(home, service=healthy, transport=answering(), port_free=lambda port: True))
    assert 'Fallback model' not in text and 'your model key' in text
