"""`wraithd onboard` in the rich look: the icon first, then pickers for every choice (prompt_toolkit's pipe input)."""

import asyncio

import httpx

from tests.cli.conftest import CTRL_D, DOWN, ENTER
from tests.fixtures.services import FakeServiceManager
from tests.test_onboard import BOT_TOKEN, FakeTelegram, KeyCheck, no_daemon
from wraithd import onboard
from wraithd.config import Home, get_key, load_config
from wraithd.daemon.service import Launchd
from wraithd.doctor import engine_name
from wraithd.tui import art


def wizard(term, **kwargs):
    home = Home.locate()
    return onboard.Wizard(home, term, service=Launchd(home, runner=FakeServiceManager()), check_key=KeyCheck(),
                          telegram_transport=httpx.MockTransport(FakeTelegram().handler),
                          health_transport=no_daemon(), **kwargs)


async def run(w):
    return await asyncio.wait_for(w.run(), 10)


async def typed(term, chunks):
    for chunk in chunks:
        await asyncio.sleep(0.15)
        term.pipe.send_text(chunk)


def test_the_icon_fits_80_columns_and_has_no_colors_of_its_own():
    lines = art.ICON.splitlines()
    assert len(lines) == 20 and max(len(line) for line in lines) <= 80
    assert set(art.ICON) <= set(' ▀▄█\n') and '\x1b' not in art.ICON


async def test_subscription_pairing_and_the_service_with_pickers(rich):
    async def login(home):
        return True, 'Claude Code uses your Claude login'

    # subscription (typed, as in plain mode), signed in, no web key; Telegram yes, token, service yes, pair, sent
    keys = ['subscription' + ENTER, ENTER, 'y' + ENTER, BOT_TOKEN + ENTER, ENTER, ENTER, ENTER]
    with rich() as term:
        w = wizard(term, check_login=login, claude_installed=lambda: (True, True))
        code, _ = await asyncio.gather(run(w), typed(term, keys))
    out = term.out.getvalue()
    assert code == 0 and out.startswith(art.ICON)
    assert '✓ How should wraithd think? subscription' in out and '✓ Signed in? I\'ve signed in: check' in out
    assert '✓ Talk to the agent from your phone through your own Telegram bot? yes' in out
    assert '✓ Sent it? I\'ve sent it: check' in out and 'Paired. The bot answers you, and only you.' in out
    assert '-- ' not in out and 'Ready ─' in out  # sections are rules, not -- titles --
    assert engine_name(Home.locate()) == 'claude-code' and get_key('telegram') == BOT_TOKEN
    assert BOT_TOKEN not in out


async def test_a_model_from_the_list_another_by_name_and_stopping(rich):
    # api-key (the default), anthropic (↓), the 3rd known model, "other…" for the strong one, a key, no check
    keys = [ENTER, DOWN + ENTER, '3', 'o' + ENTER, 'my-strong' + ENTER, 'sk-ant-test-123' + ENTER, 'n' + ENTER,
            CTRL_D]
    with rich() as term:
        code, _ = await asyncio.gather(run(wizard(term)), typed(term, keys))
    out = term.out.getvalue()
    assert code == 1 and 'Stopped. What was saved stays saved' in out  # Ctrl-D in the Telegram picker
    model = load_config(Home.locate()).model
    assert (model.provider, model.default, model.strong) == ('anthropic', 'claude-opus-5-5', 'my-strong')
    assert '✓ Model for every run claude-opus-5-5' in out and '✓ Check it with one small request' in out
    assert get_key('anthropic') == 'sk-ant-test-123'
