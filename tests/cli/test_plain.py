"""
Plain mode is the terminal as it was before the rich look, byte for byte: whenever stdin or stdout isn't a TTY,
NO_COLOR is set, WRAITHD_PLAIN=1 or `wraithd --plain`. The transcripts below were recorded from the code before the
rich look (main at ea5ff01) with the same scripted owner.
"""

import io
import re
from pathlib import Path

import httpx
import pytest

from tests.fixtures.services import FakeServiceManager
from tests.scripted import ScriptedModel, ScriptedTerminal, call, say
from tests.test_onboard import BOT_TOKEN, OPENAI_KEY, FakeTelegram, KeyCheck, no_daemon
from wraithd import cli, onboard, tui
from wraithd.channels import terminal
from wraithd.config import Home
from wraithd.core import gate as gate_module
from wraithd.daemon.service import Launchd

CHAT_PROMPTS = ['\n> ', '\n> ', 'Allow? [y/N] ', '\n> ']
CHAT_OUTPUT = (
    'wraithd. Type /new for a fresh conversation, /quit to end; what you share is remembered for next time.\n'
    'Hi.\n'
    '\n'
    'The agent wants to run skill_adjust:\n'
    '  skill: weekly-review\n'
    '  rule: Keep it to 5 lines\n'
    '  Your agent will follow this rule every time it uses this skill. You can change or remove it later.\n'
    'Not saved.\n'
)

ONBOARD_PROMPTS = [
    'How should wraithd think? (subscription | api-key | local) [api-key]: ',
    'Provider (openai | anthropic | openai-compatible) [openai]: ',
    'Model for every run [gpt-4o-mini]: ',
    'Stronger model, for skills that need one ("none" for none) [gpt-4o]: ',
    'openai API key (not shown, Enter to skip): ',
    'Check it with one small request to the provider? [Y/n]: ',
    'Talk to the agent from your phone through your own Telegram bot? [y/N]: ',
    'Bot token (not shown, Enter to skip): ',
    'Run wraithd in the background (it starts at login, restarts if it crashes)? [Y/n]: ',
    'Pair your Telegram chat with @my_agent_bot now? [Y/n]: ',
    'Press Enter once you\'ve sent it (or type "skip"): ',
]
ONBOARD_OUTPUT = (
    "wraithd setup. Enter keeps what's in [brackets]; Ctrl-D stops (what's saved stays).\n"
    '\n'
    'Workspace: <HOME>/workspace (created)\n'
    'Settings: <HOME>/config.toml\n'
    '\n'
    '-- How wraithd thinks --\n'
    'subscription: your Claude Pro or Max plan, through Claude Code (no API key)\n'
    'api-key: an OpenAI or Anthropic key (or another OpenAI-compatible endpoint)\n'
    'local: a model on this machine, through Ollama\n'
    '\n'
    '-- Model --\n'
    'Saved the openai key in the keychain.\n'
    'Check: it works.\n'
    '\n'
    '-- Web search --\n'
    'Web search: openai (your model key), about $10 per 1,000 searches on your OpenAI account.\n'
    'Optional: Google results through SerpAPI instead. Set [web] search = "serpapi" in <HOME>/config.toml '
    'and run `wraithd key set serpapi`.\n'
    '\n'
    '-- Telegram (optional) --\n'
    'In Telegram, message @BotFather, send /newbot, pick a name, and copy the token it gives.\n'
    'Saved the bot token in the keychain: @my_agent_bot.\n'
    '\n'
    '-- Background service --\n'
    'Installed: <USER>/Library/LaunchAgents/com.wraithd.daemon.plist. Logs: <HOME>/logs/daemon.log\n'
    'In Telegram, send @my_agent_bot this message:  /pair <CODE>\n'
    'Paired. The bot answers you, and only you.\n'
    '\n'
    '-- Ready --\n'
    'Model:     openai: gpt-4o-mini (strong: gpt-4o)\n'
    'Search:    openai\n'
    'Telegram:  @my_agent_bot, paired with @shahar\n'
    'Service:   running\n'
    'Web UI:    http://127.0.0.1:7878/#token=<TOKEN>\n'
    'Next: `wraithd chat` in this terminal, or message your bot. `wraithd doctor` checks everything.\n'
)


class Tty(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def no_plain_env(monkeypatch):
    """Neither variable set; afterwards they're as they were (--plain sets WRAITHD_PLAIN for the whole process)."""
    for name in ('NO_COLOR', tui.PLAIN_ENV):
        monkeypatch.setenv(name, '')  # recorded, so the test's own changes are undone
        monkeypatch.delenv(name)


def test_plain_whenever_it_is_not_a_terminal_or_the_owner_says_so(no_plain_env, monkeypatch):
    assert tui.plain(Tty(), Tty()) is False
    assert tui.plain(io.StringIO(), Tty()) and tui.plain(Tty(), io.StringIO())  # a pipe either way
    assert tui.plain(Tty(), Tty(), {'NO_COLOR': '1'}) and tui.plain(Tty(), Tty(), {'WRAITHD_PLAIN': '1'})
    assert not tui.plain(Tty(), Tty(), {'NO_COLOR': ''}) and not tui.plain(Tty(), Tty(), {'WRAITHD_PLAIN': '0'})
    assert isinstance(terminal.terminal_for(), terminal.Terminal)
    assert not terminal.terminal_for().rich  # pytest's stdin and stdout aren't terminals


def test_the_plain_flag_is_global_and_reaches_everything_it_starts(no_plain_env, capsys):
    assert cli.main(['--plain', 'runs', 'list']) == 0
    assert capsys.readouterr().out == 'No runs yet.\n'
    import os
    assert os.environ[tui.PLAIN_ENV] == '1'


def test_without_a_terminal_a_missing_id_is_an_argparse_error_as_before(capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main(['schedules', 'rm'])
    assert stopped.value.code == 2 and 'schedule_id' in capsys.readouterr().err


async def test_plain_chat_is_byte_for_byte_what_it_was(make_runtime, monkeypatch):
    monkeypatch.setattr(gate_module, 'APPROVAL_POLL_SECONDS', 0.01)
    model = ScriptedModel([[say('Hi.')],
                           [call('1', 'skill_adjust', {'skill': 'weekly-review', 'rule': 'Keep it to 5 lines'})],
                           [say('Not saved.')], [say('{"facts": []}')]])
    owner = ScriptedTerminal('hello', 'keep weekly reviews to 5 lines', 'n', '/quit')
    await terminal.chat(make_runtime(model), owner)
    assert owner.prompts == CHAT_PROMPTS
    assert owner.text == CHAT_OUTPUT


async def test_plain_chat_sends_slash_words_to_the_agent_as_before(make_runtime):
    model = ScriptedModel([[say('That is not a command I know.')], [say('{"facts": []}')]])
    owner = ScriptedTerminal('/help')
    await terminal.chat(make_runtime(model), owner)
    assert 'That is not a command I know.' in owner.text


async def test_plain_onboard_is_byte_for_byte_what_it_was():
    home = Home.locate()
    owner = ScriptedTerminal('', '', '', '', OPENAI_KEY, 'y', 'y', BOT_TOKEN, '', '', '')
    wizard = onboard.Wizard(home, owner, service=Launchd(home, runner=FakeServiceManager()), check_key=KeyCheck(),
                            telegram_transport=httpx.MockTransport(FakeTelegram().handler),
                            health_transport=no_daemon())
    assert await wizard.run() == 0
    text = owner.text.replace(str(home.root), '<HOME>').replace(str(Path.home()), '<USER>')
    text = re.sub(r'#token=[\w-]+', '#token=<TOKEN>', re.sub(r'/pair \d+', '/pair <CODE>', text))
    assert owner.prompts == ONBOARD_PROMPTS
    assert text == ONBOARD_OUTPUT
