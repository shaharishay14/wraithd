"""
The sandbox's lines in `local-agent audit` (and doctor): each broken sandbox setting is a FAIL with its fix, and the
audit exits 1 on it.
"""

import os

import pytest

from local_agent import audit, cli, doctor
from local_agent.config import load_config
from local_agent.doctor import FAIL, INFO, OK
from local_agent.mcp import config as mcp_config
from local_agent.mcp.config import McpServer
from local_agent.sandbox.audit import doctor_checks, sandbox_rules

DOCKER = '[sandbox]\nbackend = "docker"\n'


def rules(home, text=''):
    home.config_path.write_text(text)
    return sandbox_rules(home, load_config(home))


def fails(found):
    return [check.text for check in found if check.state == FAIL]


def test_off_and_docker_pass(home):
    assert [(c.state, c.text) for c in rules(home)] == [(OK, 'Sandbox: off (the shell, when on, runs as you after '
                                                             'each "y")')]
    assert [(c.state, c.text) for c in rules(home, DOCKER)] == [
        (OK, 'Sandbox: docker, the shell sees only workspace/files, no network, never unattended')]


@pytest.mark.parametrize('text, expected', [
    ('[sandbox]\nbackend = "bubblewrap"\n', "Sandbox: [sandbox] backend 'bubblewrap' is not one of none, docker; "
                                            'the shell refuses to run'),
    (DOCKER + 'network = "host"\n', "Sandbox: [sandbox] network 'host' is not one of none, bridge; the shell "
                                    'refuses to run'),
    (DOCKER + 'image = "--privileged"\n', "Sandbox: [sandbox] image '--privileged' is not a docker image name, e.g. "
                                          '"debian:bookworm-slim"; the shell refuses to run'),
    (DOCKER + 'network = "bridge"\n', 'Sandbox: the shell\'s container can reach the network ([sandbox] network = '
                                      '"bridge"), so a command can send out what it reads'),
    (DOCKER + 'mounts = ["/Users"]\n', 'Sandbox: [sandbox] mounts is not a setting and does nothing: the shell only '
                                       'ever sees workspace/files'),
    (DOCKER + 'unattended_shell = true\n', 'Sandbox: [sandbox] unattended_shell is not a setting: the shell never '
                                           'runs unattended, sandboxed or not'),
    ('[shell]\nenabled = true\nunattended = true\n' + DOCKER, 'Sandbox: [shell] unattended is not a setting: the '
                                                              'shell never runs unattended, sandboxed or not'),
])
def test_each_broken_setting_fails_with_a_fix(home, capsys, text, expected):
    found = rules(home, text)
    assert fails(found) == [expected]
    assert all(check.fix for check in found if check.state == FAIL)
    assert cli.main(['audit']) == 1 and expected in capsys.readouterr().out


def test_a_files_folder_that_widens_the_mount_fails(home, tmp_path):
    home.files_dir.rmdir()
    home.files_dir.symlink_to(home.root)  # workspace/files really is the state folder: config, state.db, token
    [text] = fails(rules(home, DOCKER))
    assert 'would mount the state folder' in text and text.endswith('the shell refuses to run')
    home.files_dir.unlink()
    home.files_dir.symlink_to(os.path.expanduser('~'))
    assert 'would mount your home folder' in fails(rules(home, DOCKER))[0]
    # only for docker: with no sandbox there's no mount to widen
    assert fails(rules(home)) == []


def test_a_files_folder_elsewhere_that_widens_nothing_passes(home, tmp_path):
    elsewhere = tmp_path / 'synced' / 'files'
    elsewhere.mkdir(parents=True)
    home.files_dir.rmdir()
    home.files_dir.symlink_to(elsewhere)
    assert fails(rules(home, DOCKER)) == []


def test_sandboxed_mcp_servers_are_listed_and_checked(home):
    mcp_config.write_server(home.config_path, McpServer(name='notes', transport='stdio', command='notes-mcp',
                                                        sandbox=True), 'notes')
    found = sandbox_rules(home, load_config(home))
    assert (INFO, 'Sandboxed MCP servers (notes): docker, nothing mounted, network none') in [
        (c.state, c.text) for c in found]
    text = home.config_path.read_text()
    home.config_path.write_text(text + '\n[sandbox]\nimage = "-v /:/host"\n')
    assert fails(sandbox_rules(home, load_config(home))) == [
        "Sandbox: [sandbox] image '-v /:/host' is not a docker image name, e.g. \"debian:bookworm-slim\"; sandboxed "
        'MCP servers (notes) won\'t start']


def test_the_audit_runs_the_sandbox_rules(home):
    home.config_path.write_text('[sandbox]\nbackend = "nope"\n')
    assert any('backend \'nope\'' in check.text for check in audit.checks(home) if check.state == FAIL)


def test_doctor_checks_for_docker_only_when_it_is_used(home, monkeypatch):
    assert doctor_checks(home, load_config(home)) == []
    home.config_path.write_text(DOCKER)
    monkeypatch.setattr('shutil.which', lambda name: None)
    [check] = doctor_checks(home, load_config(home))
    assert check.state == FAIL and 'not found' in check.text
    monkeypatch.setattr('shutil.which', lambda name: '/usr/local/bin/docker')
    assert doctor_checks(home, load_config(home))[0] == doctor.Check(
        OK, 'Docker: /usr/local/bin/docker', 'install Docker Desktop (or docker engine), or set [sandbox] backend = '
        '"none"')
