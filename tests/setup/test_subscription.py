"""
The Claude subscription's setup: onboard's choice, doctor's login check and audit's rules, all offline. Claude Code
never starts: claude_agent_sdk, wraithd.engines.claude_options and wraithd.engines.claude_code (the engine
worker's, with the contract's signatures) are fakes in sys.modules. HOME is a temp folder (conftest), with a fake
~/.claude/.credentials.json in it that nothing may open.
"""

import builtins
import importlib.util
import io
import os
import sys
import types
from pathlib import Path

import httpx
import keyring
import pytest

from tests.fixtures.services import FakeServiceManager
from tests.scripted import ScriptedTerminal
from wraithd import audit, doctor, onboard
from wraithd.config import set_key, set_values
from wraithd.daemon.service import Launchd
from wraithd.doctor import FAIL, OK, WARN

CLAUDE_TOKEN = 'sk-ant-oat01-' + 'x' * 40  # the shape of a Claude login token; wraithd must never hold one
BLOCKED_ENV = ('CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY')
INSTALL_HINT = "Claude Code isn't installed for wraithd: run `pip install 'wraithd[claude]'` (or the uv/pipx same)."
BACKGROUND = {'heartbeat', 'schedule', 'distill', 'compaction'}


class SubscriptionUnavailable(Exception):
    pass


class SystemMessage:
    def __init__(self, subtype, data):
        self.subtype, self.data = subtype, data


class ResultMessage:
    def __init__(self, is_error=False, api_error_status=None, result='OK', errors=None):
        self.is_error, self.api_error_status, self.result, self.errors = is_error, api_error_status, result, errors


class FakeClaude:
    """claude_agent_sdk and engines.claude_code as the contract has them. `source` is the init's apiKeySource;
    `installed=False` makes claude_options raise SubscriptionUnavailable, like a missing extra."""

    def __init__(self, source='none', installed=True, options=None, result=None):
        self.source, self.installed, self.queries, self.closed = source, installed, [], 0
        self.result, self.read = result or ResultMessage(), []
        self.options = options or {}

    def claude_options(self, instructions, server, names, model, stream=False):
        if not self.installed:
            raise SubscriptionUnavailable(INSTALL_HINT)
        if not model:
            raise ValueError('a model is required')
        return types.SimpleNamespace(**{
            'system_prompt': instructions, 'tools': [], 'mcp_servers': {'wraithd': server} if server else {},
            'allowed_tools': names, 'strict_mcp_config': True, 'setting_sources': [],
            'env': {'ANTHROPIC_API_KEY': ''}, 'max_turns': 40, 'model': model, 'include_partial_messages': stream,
            **self.options})

    @staticmethod
    def model_for(engine_config, purpose):
        return engine_config['background_model'] if purpose in BACKGROUND else engine_config['chat_model']

    @staticmethod
    def subscription_env_problem():
        for var in BLOCKED_ENV:
            if os.environ.get(var, '').strip().lower() not in ('', '0', 'false', 'no', 'off'):
                return f'{var} is set, so Claude Code would not use your Claude login: unset it'
        return None

    def query(self, prompt, options):
        self.queries.append((prompt, options))
        fake = self

        async def messages():
            try:
                fake.read.append('init')
                yield SystemMessage('init', {'apiKeySource': fake.source, 'model': 'claude-haiku-5-5'})
                fake.read.append('result')
                yield fake.result
                raise AssertionError('the check reads past the result')
            finally:
                fake.closed += 1
        return messages()

    def install(self, monkeypatch):
        sdk = types.ModuleType('claude_agent_sdk')
        sdk.SystemMessage, sdk.ResultMessage, sdk.query = SystemMessage, ResultMessage, self.query
        options = types.ModuleType('wraithd.engines.claude_options')
        options.claude_options, options.model_for, options.LOGIN = self.claude_options, self.model_for, 'none'
        options.subscription_env_problem = self.subscription_env_problem
        engine = types.ModuleType('wraithd.engines.claude_code')
        engine.SubscriptionUnavailable = SubscriptionUnavailable
        monkeypatch.setitem(sys.modules, 'claude_agent_sdk', sdk)
        if importlib.util.find_spec('wraithd.engines') is None:  # before the engine branch is merged
            package = types.ModuleType('wraithd.engines')
            package.__path__ = []
            monkeypatch.setitem(sys.modules, 'wraithd.engines', package)
        monkeypatch.setitem(sys.modules, 'wraithd.engines.claude_options', options)
        monkeypatch.setitem(sys.modules, 'wraithd.engines.claude_code', engine)
        return self


@pytest.fixture
def claude(monkeypatch):
    for name in BLOCKED_ENV + ('CLAUDE_CODE_OAUTH_TOKEN',):
        monkeypatch.delenv(name, raising=False)
    return FakeClaude().install(monkeypatch)


def on(home):
    set_values(home, 'engine', {'name': 'claude-code'})


def no_daemon():
    def refuse(request):
        raise httpx.ConnectError('connection refused')
    return httpx.MockTransport(refuse)


def doctor_lines(home):
    found = doctor.checks(home, service=Launchd(home, runner=FakeServiceManager()), transport=no_daemon(),
                          port_free=lambda port: True)
    return {check.text: check.state for check in found}, doctor.render(found)


# --- onboard ------------------------------------------------------------------------------------------------

async def test_onboard_subscription_writes_claude_code_through_the_real_login_check(home, claude):
    term = ScriptedTerminal('subscription', '', 'n', 'n')
    wizard = onboard.Wizard(home, term, service=Launchd(home, runner=FakeServiceManager()),
                            health_transport=no_daemon(), claude_installed=lambda: (True, True))
    assert await wizard.run() == 0
    assert term.lines == []
    assert '[engine]\nname = "claude-code"' in home.config_path.read_text()
    assert 'Check: Claude subscription login in use.' in term.text
    assert not [prompt for prompt in term.prompts if 'not shown' in prompt]  # no secret was asked for
    assert keyring.get_keyring().passwords == {}  # and nothing went to the keychain
    [(prompt, options)] = claude.queries
    assert options.model == 'haiku' and options.tools == [] and options.allowed_tools == [] and claude.closed == 1


# --- doctor -------------------------------------------------------------------------------------------------

def test_doctor_reports_the_subscription_login_and_needs_no_key(home, claude):
    on(home)
    lines, text = doctor_lines(home)
    assert lines['Claude subscription login in use'] == OK
    assert lines['Claude subscription: no API key needed (Claude Code uses your own login)'] == OK
    assert not [line for line in lines if line.startswith('openai key')]
    [(prompt, options)] = claude.queries
    assert prompt == options.system_prompt == 'Reply with OK.' and options.model == 'haiku'
    assert (options.tools, options.mcp_servers, options.allowed_tools) == ([], {}, [])
    assert claude.read == ['init', 'result'] and claude.closed == 1  # one short reply, then Claude Code is closed


@pytest.mark.parametrize('result', [
    ResultMessage(is_error=True, api_error_status=401, result='Invalid API key · Please run /login'),
    ResultMessage(is_error=True, api_error_status=403),
    ResultMessage(is_error=True, result='Not logged in · Please run /login'),
    ResultMessage(is_error=True, errors=['OAuth token has expired']),
])
def test_doctor_says_when_claude_code_is_not_signed_in_at_all(home, monkeypatch, result):
    """apiKeySource reads none with nobody signed in, so only the reply tells: this must never pass."""
    fake = FakeClaude(result=result).install(monkeypatch)
    on(home)
    [check] = doctor.subscription_checks(home)
    assert check == doctor.Check(FAIL, 'Claude Code isn\'t signed in: run `claude` and type /login',
                                 'see docs/claude-subscription.md')
    assert fake.read == ['init', 'result']


def test_doctor_at_the_plan_limit_is_signed_in_with_a_warning(home, monkeypatch):
    FakeClaude(result=ResultMessage(is_error=True, api_error_status=429, result='rate limited')).install(monkeypatch)
    on(home)
    [check] = doctor.subscription_checks(home)
    assert check.state == WARN and 'signed in, but your plan limit is reached right now' in check.text
    lines, text = doctor_lines(home)
    assert 'Claude subscription login in use: signed in, but your plan limit is reached right now' in lines
    assert 'fix: wait for the limit to reset' in text


def test_doctor_shows_any_other_error_as_it_came(home, monkeypatch):
    FakeClaude(result=ResultMessage(is_error=True, api_error_status=529, errors=['Overloaded'])).install(monkeypatch)
    on(home)
    [check] = doctor.subscription_checks(home)
    assert check.state == FAIL and check.text == 'Claude Code answered with an error: Overloaded'


def test_doctor_fails_when_a_key_wins_over_the_login(home, monkeypatch):
    fake = FakeClaude(source='ANTHROPIC_API_KEY').install(monkeypatch)
    on(home)
    lines, text = doctor_lines(home)
    assert lines['Claude Code is using ANTHROPIC_API_KEY, not your login: unset ANTHROPIC_API_KEY where wraithd '
                 'runs, then run `claude` and type /login'] == FAIL
    assert 'fix: see docs/claude-subscription.md' in text
    assert fake.read == ['init'] and fake.closed == 1  # not the login: no reply is spent on it


@pytest.mark.parametrize('source, fix', [
    ('apiKeyHelper', 'remove apiKeyHelper from your Claude Code settings'),
    ('/login managed key', 'Anthropic Console account (API billing), not a Claude plan'),
])
def test_doctor_names_the_fix_for_each_other_credential(home, monkeypatch, source, fix):
    FakeClaude(source=source).install(monkeypatch)
    on(home)
    [check] = doctor.subscription_checks(home)
    assert check.state == FAIL and fix in check.text


def test_doctor_uses_the_background_model_the_owner_set(home, claude):
    set_values(home, 'engine', {'name': 'claude-code', 'background_model': 'claude-haiku-5-5'})
    doctor.subscription_checks(home)
    assert claude.queries[0][1].model == 'claude-haiku-5-5'


def test_doctor_refuses_bedrock_and_vertex(home, claude, monkeypatch):
    on(home)
    monkeypatch.setenv('CLAUDE_CODE_USE_BEDROCK', '0')  # off
    assert doctor.subscription_checks(home)[0].state == OK
    for name in ('CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX'):
        monkeypatch.setenv(name, '1')
        [check] = doctor.subscription_checks(home)
        assert check.state == FAIL
        assert check.text == f'{name} is set, so Claude Code would not use your Claude login: unset it'
        monkeypatch.delenv(name)
    assert len(claude.queries) == 1  # only the '0' run started Claude Code; the others were refused first


def test_doctor_without_the_sdk_says_how_to_install_the_extra(home, monkeypatch):
    monkeypatch.setitem(sys.modules, 'wraithd.engines.claude_options', None)  # importing it fails, as without the SDK
    on(home)
    [check] = doctor.subscription_checks(home)
    assert check.state == FAIL and check.text.startswith('Claude Code extra not installed')
    assert "uv tool install 'wraithd[claude]'" in check.text


def test_doctor_without_the_extra_says_how_to_install_it(home, monkeypatch):
    FakeClaude(installed=False).install(monkeypatch)
    on(home)
    [check] = doctor.subscription_checks(home)
    assert check.state == FAIL
    assert check.text == INSTALL_HINT


def test_doctor_on_the_api_engine_never_starts_claude_code(home, claude):
    lines, _ = doctor_lines(home)
    assert claude.queries == [] and lines['openai key: missing'] == FAIL
    assert not [line for line in lines if 'Claude' in line]


async def test_the_login_check_also_works_inside_a_running_event_loop(home, claude):
    on(home)
    [check] = doctor.subscription_checks(home)
    assert check == doctor.Check(OK, 'Claude subscription login in use')


# --- audit --------------------------------------------------------------------------------------------------

def rules(home):
    return {check.text: check for check in audit.checks(home)}


def test_audit_with_claude_code_on_passes(home, claude):
    on(home)
    found = rules(home)
    assert found['wraithd holds no Claude login token'].state == OK
    assert found['Claude Code isn\'t sent to Bedrock or Vertex'].state == OK
    assert found['Claude Code gets only wraithd\'s guarded tools, no other settings'].state == OK
    assert not [check for check in found.values() if check.state in (FAIL, WARN)]
    assert claude.queries == []  # audit only builds the options; it never starts Claude Code


@pytest.mark.parametrize('loose, named', [
    ({'tools': None}, 'its own tools are on'),
    ({'tools': ['Bash']}, 'its own tools are on'),
    ({'strict_mcp_config': False}, 'strict_mcp_config is off'),
    ({'setting_sources': ['user', 'project']}, 'your Claude Code settings load'),
    ({'env': {'CLAUDE_CODE_OAUTH_TOKEN': 'x'}}, 'CLAUDE_CODE_OAUTH_TOKEN'),
])
def test_audit_fails_if_the_engine_options_loosen(home, monkeypatch, loose, named):
    FakeClaude(options=loose).install(monkeypatch)
    on(home)
    [check] = [check for check in audit.checks(home) if check.text.startswith('Claude Code isn\'t locked down')]
    assert check.state == FAIL and named in check.text


def test_audit_fails_with_bedrock_or_vertex_set(home, claude, monkeypatch):
    on(home)
    monkeypatch.setenv('CLAUDE_CODE_USE_VERTEX', '1')
    check = rules(home)['CLAUDE_CODE_USE_VERTEX is set, so Claude Code would not use your Claude login: unset it']
    assert check.state == FAIL and check.fix == 'unset that variable where wraithd runs (or set it to 0)'
    assert audit.main(home) == 1


def test_audit_warns_about_anthropic_api_key_in_the_environment(home, claude, monkeypatch):
    on(home)
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-ant-api03-' + 'k' * 30)
    [check] = [check for check in audit.checks(home) if check.text.startswith('ANTHROPIC_API_KEY is set')]
    assert check.state == WARN and 'unset ANTHROPIC_API_KEY' in check.fix
    assert audit.main(home) == 0  # a warning, not a failure
    monkeypatch.delenv('ANTHROPIC_API_KEY')
    on(home)
    assert not [check for check in audit.checks(home) if check.text.startswith('ANTHROPIC_API_KEY')]


def test_audit_on_the_api_engine_leaves_anthropic_api_key_alone(home, claude, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-ant-api03-' + 'k' * 30)
    texts = list(rules(home))
    assert not [text for text in texts if 'ANTHROPIC_API_KEY' in text or text.startswith('Claude Code')]


def test_audit_fails_on_a_claude_token_in_the_config(home, claude):
    home.config_path.write_text(home.config_path.read_text() + f'\n# pasted by mistake: {CLAUDE_TOKEN}\n')
    check = rules(home)['wraithd holds a Claude login token (config.toml): it must never have one']
    assert check.state == FAIL and audit.main(home) == 1


def test_audit_fails_on_a_claude_token_in_wraithds_keychain(home, claude):
    set_key('anthropic', CLAUDE_TOKEN)
    check = rules(home)['wraithd holds a Claude login token (keychain anthropic): it must never have one']
    assert check.state == FAIL and 'wraithd key rm' in check.fix
    set_key('anthropic', 'sk-ant-api03-' + 'k' * 30)  # an API key is fine
    assert rules(home)['wraithd holds no Claude login token'].state == OK


def test_audit_finds_a_claude_token_in_an_mcp_servers_value(home, claude):
    from wraithd.mcp.config import set_secret

    with home.config_path.open('a') as config:
        config.write('\n[mcp.servers.notes]\ncommand = "notes-mcp"\nenv = ["NOTES_TOKEN"]\n')
    set_secret('notes', 'env', 'NOTES_TOKEN', CLAUDE_TOKEN)
    assert rules(home)['wraithd holds a Claude login token (keychain mcp/notes/env/NOTES_TOKEN): it must never '
                       'have one'].state == FAIL


# --- never Claude Code's credentials ------------------------------------------------------------------------

def test_doctor_and_audit_never_open_claude_codes_credentials_or_set_its_token(home, claude, monkeypatch):
    claude_dir = Path.home() / '.claude'  # the temp HOME (conftest)
    claude_dir.mkdir(parents=True)
    credentials = claude_dir / '.credentials.json'
    credentials.write_text(f'{{"claudeAiOauth": {{"accessToken": "{CLAUDE_TOKEN}"}}}}')
    opened, services = [], []

    def watch(original):
        def wrapper(file, *args, **kwargs):
            if isinstance(file, str | bytes | os.PathLike):
                opened.append(os.fsdecode(file))
            return original(file, *args, **kwargs)
        return wrapper

    monkeypatch.setattr(builtins, 'open', watch(builtins.open))
    monkeypatch.setattr(io, 'open', watch(io.open))
    monkeypatch.setattr(os, 'open', watch(os.open))
    real_get = keyring.get_password
    monkeypatch.setattr(keyring, 'get_password', lambda service, account: (services.append(service),
                                                                           real_get(service, account))[1])
    on(home)
    environ_before = dict(os.environ)

    _, text = doctor_lines(home)
    audit.checks(home)
    assert 'Claude subscription login in use' in text

    assert opened and not [path for path in opened if '.claude' in Path(path).parts]
    assert set(services) <= {'wraithd', 'local-agent'}  # wraithd's own keychain entries, never Claude Code's
    assert 'CLAUDE_CODE_OAUTH_TOKEN' not in os.environ and dict(os.environ) == environ_before
    for _, options in claude.queries:
        assert 'CLAUDE_CODE_OAUTH_TOKEN' not in options.env and options.env['ANTHROPIC_API_KEY'] == ''
    assert CLAUDE_TOKEN not in text


def test_the_setup_code_never_names_claude_codes_credentials():
    """Belt and braces for the test above: the files never name the credentials file or Claude Code's keychain
    entry, and never set an environment variable."""
    root = Path(doctor.__file__).parent
    for name in ('doctor.py', 'audit.py', 'onboard.py'):
        code = (root / name).read_text()
        assert '.credentials.json' not in code
        assert 'Claude Code-credentials' not in code
        assert 'environ[\'CLAUDE_CODE_OAUTH_TOKEN\']' not in code and 'setenv' not in code


def test_audit_without_the_extra_fails_with_the_install_hint(home, monkeypatch):
    FakeClaude(installed=False).install(monkeypatch)
    on(home)
    check = rules(home)[f'The engine can\'t run: {INSTALL_HINT}']
    assert check.state == FAIL and check.fix == "uv tool install 'wraithd[claude]'"
