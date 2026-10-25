"""
`local-agent onboard` end to end with scripted answers: an in-memory keychain, a fake Telegram, a fake service
manager, and a temp HOME and LOCAL_AGENT_HOME (conftest). The key check is a fake that records its calls: the
real one would call the provider.
"""

import json

import httpx
import pytest

from local_agent import onboard
from local_agent.channels import telegram
from local_agent.config import get_key, load_config, set_key
from local_agent.daemon.service import Launchd
from local_agent.store import SQLiteStore
from tests.fixtures.services import FakeServiceManager
from tests.scripted import ScriptedTerminal

BOT_TOKEN = '123456:fake-bot-token-abcdefghij'
OPENAI_KEY, SERP_KEY = 'sk-test-onboard-0123456789', 'serp-test-0123456789'
OWNER = 1001


class FakeTelegram:
    """getMe for the token check; getUpdates hands out `/pair <the code>` once a code is waiting, like the owner
    typing it in their chat; sendMessage is recorded."""

    def __init__(self, token=BOT_TOKEN):
        self.token, self.calls, self.next_update = token, [], 1

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit('/', 1)[1]
        params = json.loads(request.content or b'{}')
        self.calls.append((method, params))
        if not request.url.path.startswith(f'/bot{self.token}/'):
            return httpx.Response(401, json={'ok': False, 'description': 'Unauthorized'})
        if method == 'getMe':
            return httpx.Response(200, json={'ok': True, 'result': {'username': 'my_agent_bot'}})
        if method == 'getUpdates':
            result = []
            store = SQLiteStore(onboard_home().db_path)
            pairing = json.loads(store.get_setting(telegram.PAIRING) or 'null')
            store.close()
            if pairing and params.get('offset', 0) <= self.next_update:
                result = [{'update_id': self.next_update, 'message': {
                    'message_id': 1, 'text': f'/pair {pairing["code"]}',
                    'chat': {'id': OWNER, 'type': 'private', 'username': 'shahar'}}}]
                self.next_update += 1
            return httpx.Response(200, json={'ok': True, 'result': result})
        return httpx.Response(200, json={'ok': True, 'result': {'message_id': 7}})

    def sent(self):
        return [params['text'] for method, params in self.calls if method == 'sendMessage']


def onboard_home():
    from local_agent.config import Home
    return Home.locate()


class KeyCheck:
    def __init__(self, result=(True, 'it works')):
        self.result, self.calls = result, []

    def __call__(self, provider, key, base_url=''):
        self.calls.append((provider, key, base_url))
        return self.result


def no_daemon():
    def refuse(request):
        raise httpx.ConnectError('connection refused')
    return httpx.MockTransport(refuse)


@pytest.fixture
def manager():
    return FakeServiceManager()


def wizard(term, manager, fake=None, check=None, health=None):
    home = onboard_home()
    return onboard.Wizard(home, term, service=Launchd(home, runner=manager), check_key=check or KeyCheck(),
                          telegram_transport=httpx.MockTransport((fake or FakeTelegram()).handler),
                          health_transport=health or no_daemon())


async def test_onboard_from_nothing_to_a_paired_bot_and_a_running_service(manager):
    fake, check = FakeTelegram(), KeyCheck()
    term = ScriptedTerminal(
        '',             # provider [openai]
        '',             # model [gpt-4o-mini]
        '',             # strong model [gpt-4o]
        OPENAI_KEY,     # openai API key (not shown)
        'y',            # check it
        'y', SERP_KEY,  # web search key
        'y', BOT_TOKEN,  # Telegram bot
        '',             # run in the background [Y/n]
        '',             # pair now [Y/n]
        '',             # Enter once sent
    )
    assert await wizard(term, manager, fake, check).run() == 0
    assert term.lines == []  # every answer was asked for

    home = onboard_home()
    assert home.workspace.is_dir() and home.agents_md.exists()
    config = load_config(home)
    assert (config.model.provider, config.model.default, config.model.strong) == ('openai', 'gpt-4o-mini', 'gpt-4o')
    assert (get_key('openai'), get_key('serpapi'), get_key('telegram')) == (OPENAI_KEY, SERP_KEY, BOT_TOKEN)
    assert check.calls == [('openai', OPENAI_KEY, '')]
    assert manager.loaded and manager.running and Launchd(home, runner=manager).path.is_file()

    store = SQLiteStore(home.db_path)
    assert store.get_setting(telegram.OWNER_CHAT) == str(OWNER) and store.get_setting(telegram.PAIRING) is None
    store.close()
    assert fake.sent() == ['Paired: this chat is now the owner\'s.']

    text = term.text
    assert 'Check: it works.' in text and 'In Telegram, send @my_agent_bot this message:  /pair ' in text
    token = (home.root / 'api-token').read_text().strip()
    assert f'Web UI:    http://127.0.0.1:7878/#token={token}' in text
    assert 'Telegram:  @my_agent_bot, paired with @shahar' in text and 'Service:   running' in text
    for secret in (OPENAI_KEY, SERP_KEY, BOT_TOKEN):  # keys are typed without echo and never printed
        assert secret not in text


async def test_running_it_again_with_enter_everywhere_changes_nothing(manager):
    assert await wizard(ScriptedTerminal('', '', '', OPENAI_KEY, 'n', 'y', SERP_KEY, 'y', BOT_TOKEN, '', '', ''),
                        manager).run() == 0
    home = onboard_home()
    config_before = home.config_path.read_text()

    term = ScriptedTerminal(
        '', '', '',  # provider, model, strong model: as they are
        '',          # replace the openai key? [y/N]
        'n',         # check it
        '',          # replace the serpapi key? [y/N]
        '',          # replace the bot token? [y/N]
        '',          # restart the service so the settings apply [Y/n]
    )                # already paired: no pairing question
    assert await wizard(term, manager).run() == 0
    assert term.lines == []
    assert home.config_path.read_text() == config_before
    assert (get_key('openai'), get_key('serpapi'), get_key('telegram')) == (OPENAI_KEY, SERP_KEY, BOT_TOKEN)
    assert manager.commands('launchctl')[-1][:3] == ['launchctl', 'kickstart', '-k']
    assert 'Telegram:  @my_agent_bot, paired with @shahar' in term.text


async def test_ollama_needs_no_key_and_the_settings_keep_their_comments(manager):
    check = KeyCheck((False, 'could not reach ollama (ConnectError)'))
    term = ScriptedTerminal('ollama', '', '', 'none', 'y', 'n', 'n', 'n')
    assert await wizard(term, manager, check=check).run() == 0
    assert term.lines == []
    home = onboard_home()
    config = load_config(home)
    assert (config.model.provider, config.model.base_url, config.model.default, config.model.strong) == (
        'ollama', 'http://localhost:11434/v1', 'llama3.2', '')
    text = home.config_path.read_text()
    assert 'provider = "ollama"          # openai | anthropic | ollama | openai-compatible' in text
    assert check.calls == [('ollama', None, 'http://localhost:11434/v1')]
    assert 'Check failed: could not reach ollama' in term.text
    assert 'Service:   not installed' in term.text and manager.commands('launchctl') == []


async def test_a_bad_bot_token_is_not_saved(manager):
    term = ScriptedTerminal('', '', '', OPENAI_KEY, 'n', 'n', 'y', '999:wrong-token', '', 'n')
    assert await wizard(term, manager).run() == 0
    assert term.lines == []
    assert get_key('telegram') is None
    assert 'Telegram getMe failed: Unauthorized Check the token and try again.' in term.text
    assert 'Telegram:  off' in term.text


async def test_an_invalid_answer_is_asked_again(manager):
    term = ScriptedTerminal('gemini', 'anthropic', '', '', '', 'n', 'maybe', 'n', 'n')
    assert await wizard(term, manager).run() == 0
    assert term.lines == []
    assert 'That isn\'t one of: openai, anthropic, ollama, openai-compatible.' in term.text
    assert 'Please answer y or n.' in term.text
    config = load_config(onboard_home())
    assert (config.model.provider, config.model.default) == ('anthropic', 'claude-haiku-5-5')
    assert 'No key yet: runs won\'t work until you set one (local-agent key set anthropic).' in term.text


async def test_ctrl_d_stops_and_keeps_what_was_saved(manager):
    term = ScriptedTerminal('', '', '', OPENAI_KEY)  # then input ends
    assert await wizard(term, manager).run() == 1
    assert get_key('openai') == OPENAI_KEY and 'Stopped. What was saved stays saved' in term.text


async def test_with_a_daemon_running_pairing_waits_for_it(manager, monkeypatch):
    """The daemon polls Telegram itself (two pollers would conflict): onboard makes the code and watches the store."""
    set_key('openai', OPENAI_KEY)
    set_key('telegram', BOT_TOKEN)
    monkeypatch.setattr(onboard, 'DAEMON_PAIRING_WAIT', 1)
    running = httpx.MockTransport(lambda request: httpx.Response(200, json={'ok': True, 'version': '0.0.1'}))

    class OwnerPairsOnTheirPhone(ScriptedTerminal):
        async def ask(self, prompt):
            if prompt.startswith('Press Enter'):  # meanwhile, the daemon handles /pair
                store = SQLiteStore(onboard_home().db_path)
                store.set_setting(telegram.OWNER_CHAT, str(OWNER))
                store.set_setting(telegram.OWNER_NAME, '@shahar')
                store.close()
            return await super().ask(prompt)

    fake = FakeTelegram()
    term = OwnerPairsOnTheirPhone('', '', '', '', 'n', 'n', '', 'n', '', '')
    assert await wizard(term, manager, fake, health=running).run() == 0
    assert term.lines == []
    assert 'Paired. The bot answers you, and only you.' in term.text
    assert not [method for method, _ in fake.calls if method == 'getUpdates']  # onboard didn't poll
