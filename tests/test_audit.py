"""
`local-agent audit`: the live setup against the security rules. A fresh home passes; each broken rule is a FAIL
with its fix, and the exit code says so. Standing permissions show as info.
"""

from datetime import UTC, datetime

import pytest

from local_agent import audit, cli
from local_agent.config import Home
from local_agent.core import gate
from local_agent.daemon import app as daemon_app
from local_agent.doctor import FAIL, INFO, OK
from local_agent.store import SQLiteStore


def states(found):
    return {check.text: check.state for check in found}


def fixes(found):
    return {check.text: check.fix for check in found if check.state == FAIL}


def test_a_fresh_home_passes_every_rule(home, capsys):
    assert cli.main(['audit']) == 0
    text = capsys.readouterr().out
    for rule in (f'ok    {home.root} is yours alone (mode 700)', 'ok    Nothing in it is writable by other accounts',
                 'ok    The API binds to 127.0.0.1 only', 'ok    API token: not made yet',
                 'ok    Only the daemon\'s own origin may call the API', 'ok    Shell: off',
                 'ok    No keys in config.toml'):
        assert rule in text, rule
    assert text.endswith('All good.\n')


def test_each_broken_rule_is_a_fail_with_its_fix(home, monkeypatch, capsys):
    home.root.chmod(0o755)
    (home.memory_dir / 'shared.md').write_text('anyone can edit this')
    (home.memory_dir / 'shared.md').chmod(0o666)
    (home.root / 'api-token').write_text('short\n')
    (home.root / 'api-token').chmod(0o644)
    home.config_path.write_text('[shell]\nenabled = true\n\n[model]\n'
                                'default = "sk-proj-abcdefghijklmnopqrstuvwxyz0123"\n')  # a pasted key
    monkeypatch.setattr(gate, 'NEVER_UNATTENDED', set())  # a build where the shell could run unattended

    found = audit.checks(home)
    assert fixes(found) == {
        f'{home.root} is yours alone (mode 755)': f'chmod 700 {home.root}',
        'Writable by other accounts: workspace/memory/shared.md': f'chmod -R go-w {home.root}',
        'API token is too short': f'rm {home.root / "api-token"} and restart the daemon (it makes a new one)',
        'Shell: on, and not every command asks first': f'set shell_enabled = false in {home.config_path}',
        'config.toml holds what looks like a key (openai_key)':
            f'remove it from {home.config_path}; keep it with local-agent key set <provider> or mcp add --env',
    }
    assert cli.main(['audit']) == 1 and '5 problems found.' in capsys.readouterr().out


def test_a_private_token_and_a_gated_shell_pass(home):
    (home.root / 'api-token').write_text('t' * 43 + '\n')
    (home.root / 'api-token').chmod(0o600)
    home.config_path.write_text('[shell]\nenabled = true\n')
    found = states(audit.checks(home))
    assert found['API token is set and private (mode 600)'] == OK
    assert found['Shell: on, every command asks first and never runs unattended'] == OK


def test_an_api_on_every_interface_fails(home, monkeypatch):
    monkeypatch.setattr(daemon_app, 'HOST', '0.0.0.0')
    assert fixes(audit.checks(home)) == {
        'The API binds to 0.0.0.0, which other machines can reach': 'reinstall Local Agent'}


@pytest.mark.parametrize('origin, state', [('http://localhost:5173', INFO), ('http://127.0.0.1:3000/', INFO),
                                           ('http://localhost:5173.evil.example', FAIL)])
def test_a_dev_origin_must_be_on_this_machine(home, origin, state):
    home.config_path.write_text(f'[daemon]\ndev_origin = "{origin}"\n')
    [check] = [check for check in audit.checks(home) if origin in check.text]
    assert check.state == state


def test_standing_permissions_are_listed(home):
    home.config_path.write_text('[mcp.servers.gmail]\ncommand = "gmail-mcp"\nalways_allow = ["send_email"]\n')
    store = SQLiteStore(home.db_path)
    store.add_schedule('morning brief', '0 8 * * 1-5', None, 'UTC', next_fire_at=datetime.now(UTC))
    store.close()
    info = [check.text for check in audit.checks(home) if check.state == INFO]
    assert any('gmail send_email' in text for text in info)
    assert '1 schedule runs unattended: local-agent schedules list' in info


def test_without_a_home_or_with_a_broken_config(home, tmp_path, capsys):
    home.config_path.write_text('not toml [')
    assert any(text.startswith('Settings don\'t load') for text in fixes(audit.checks(home)))
    assert fixes(audit.checks(Home(tmp_path / 'nowhere'))) == {f'No state folder at {tmp_path / "nowhere"}':
                                                               'local-agent onboard'}
