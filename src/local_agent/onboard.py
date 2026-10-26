"""
`local-agent onboard`: from nothing to a working agent, one question at a time.

    workspace -> model and key (with an optional check) -> web search -> Telegram bot -> background service
    -> Telegram pairing -> a summary with the web UI link

Safe to run again: every question defaults to what's already set (Enter keeps it), a key already in the keychain
is only replaced when the owner says so, and the config file keeps its comments (config.set_values). Ctrl-D stops
the wizard; what was saved before that stays saved.

The key check makes one small request to the provider (its model list, not a model call). It's asked for, can be
skipped, and is passed in as `check_key` so tests never make it.
"""

import asyncio
import sys
from dataclasses import dataclass, field
from types import SimpleNamespace

import httpx

from local_agent.config import (
    PROVIDERS,
    ConfigError,
    Home,
    get_key,
    load_config,
    set_key,
    set_values,
)
from local_agent.daemon import service as service_module
from local_agent.daemon.service import ServiceError
from local_agent.providers import OLLAMA_URL

# What a new provider starts with; the owner can type any model name instead
MODELS = {'openai': ('gpt-4o-mini', 'gpt-4o'), 'anthropic': ('claude-haiku-5-5', 'claude-sonnet-5-5'),
          'ollama': ('llama3.2', ''), 'openai-compatible': ('', '')}
NEEDS_KEY = {'openai', 'anthropic'}  # ollama needs none; a self-hosted endpoint may or may not
PAIRING_CHECKS = 3  # how many times the owner can press Enter before pairing is left for later
DAEMON_PAIRING_WAIT = 15  # seconds to wait for a running daemon to see the /pair message


class Stopped(Exception):
    """The owner ended input (Ctrl-D)."""


def check_key(provider: str, key: str | None, base_url: str = '') -> tuple[bool, str]:
    """One request for the provider's model list: does the key (or the local endpoint) work?"""
    if provider == 'openai':
        url, headers = 'https://api.openai.com/v1/models', {'Authorization': f'Bearer {key}'}
    elif provider == 'anthropic':
        url, headers = 'https://api.anthropic.com/v1/models', {'x-api-key': key or '',
                                                               'anthropic-version': '2023-06-01'}
    else:
        url = (base_url or OLLAMA_URL).rstrip('/') + '/models'
        headers = {'Authorization': f'Bearer {key}'} if key else {}
    try:
        response = httpx.get(url, headers=headers, timeout=10)
    except httpx.HTTPError as e:
        return False, f'could not reach {provider} ({type(e).__name__})'
    if response.status_code == 200:
        return True, 'it works'
    if response.status_code in (401, 403):
        return False, f'{provider} turned the key down (HTTP {response.status_code})'
    return False, f'{provider} answered HTTP {response.status_code}'


@dataclass
class Wizard:
    home: Home
    term: object  # channels.terminal.Terminal: ask, secret, say
    service: object | None = None  # daemon.service.Service; None: this system's, if it has one
    check_key: object = check_key
    telegram_transport: httpx.AsyncBaseTransport | None = None
    health_transport: httpx.BaseTransport | None = None
    summary: dict = field(default_factory=dict)

    # --- asking ----------------------------------------------------------------------------------------------

    async def ask(self, question: str, default: str = '') -> str:
        answer = await self.term.ask(f'{question}{f" [{default}]" if default else ""}: ')
        if answer is None:
            raise Stopped
        return answer.strip() or default

    async def yes(self, question: str, default: bool) -> bool:
        while True:
            answer = (await self.ask(f'{question} {"[Y/n]" if default else "[y/N]"}')).lower()
            if not answer:
                return default
            if answer in ('y', 'yes', 'n', 'no'):
                return answer.startswith('y')
            self.term.say('Please answer y or n.')

    async def choose(self, question: str, choices, default: str) -> str:
        while True:
            answer = await self.ask(f'{question} ({" | ".join(choices)})', default)
            if answer in choices:
                return answer
            self.term.say(f'That isn\'t one of: {", ".join(choices)}.')

    async def secret(self, question: str) -> str:
        answer = await self.term.secret(f'{question} (not shown, Enter to skip): ')
        if answer is None:
            raise Stopped
        return answer.strip()

    async def maybe_key(self, name: str, what: str) -> bool:
        """Asks for a key unless one is set and the owner keeps it. True if a key is set afterwards."""
        if get_key(name) and not await self.yes(f'{what} is already in the keychain. Replace it?', False):
            return True
        value = await self.secret(what)
        if value:
            set_key(name, value)
            self.term.say(f'Saved the {name} key in the keychain.')
        return bool(value) or bool(get_key(name))

    # --- the steps -------------------------------------------------------------------------------------------

    def workspace(self) -> None:
        existed = self.home.workspace.exists()
        self.home.ensure()
        self.term.say(f'Workspace: {self.home.workspace} ({"already there" if existed else "created"})')
        self.term.say(f'Settings: {self.home.config_path}')

    async def model(self) -> None:
        self.term.say('\n-- Model --')
        current = load_config(self.home).model
        provider = await self.choose('Provider', PROVIDERS, current.provider)
        default, strong = (current.default, current.strong) if provider == current.provider else MODELS[provider]
        values = {'provider': provider}
        if provider in ('ollama', 'openai-compatible'):
            base_url = current.base_url or (OLLAMA_URL if provider == 'ollama' else '')
            while not (base_url := await self.ask('Endpoint URL', base_url)):
                self.term.say('An openai-compatible endpoint needs its URL, e.g. http://localhost:8000/v1.')
            values['base_url'] = base_url
        while not (default := await self.ask('Model for every run', default)):
            self.term.say('Type the model\'s name, as the endpoint knows it.')
        values['default'] = default
        answer = await self.ask('Stronger model, for skills that need one ("none" for none)', strong or 'none')
        values['strong'] = '' if answer == 'none' else answer
        set_values(self.home, 'model', values)
        self.summary['model'] = f'{provider}: {default}' + (f' (strong: {values["strong"]})' if values['strong']
                                                            else '')

        has_key = True
        if provider in NEEDS_KEY or provider == 'openai-compatible':
            what = f'{provider} API key' + (' (if the endpoint wants one)' if provider not in NEEDS_KEY else '')
            has_key = await self.maybe_key(provider, what)
            if not has_key and provider in NEEDS_KEY:
                self.term.say(f'No key yet: runs won\'t work until you set one (local-agent key set {provider}).')
        if has_key and await self.yes('Check it with one small request to the provider?', True):
            ok, message = self.check_key(provider, get_key(provider), values.get('base_url', ''))
            self.term.say(f'Check: {message}.' if ok else f'Check failed: {message}. You can fix it later with '
                                                          f'`local-agent key set {provider}` and `local-agent doctor`.')

    async def search(self) -> None:
        """Nothing to ask by default: search uses the model provider's own (or DuckDuckGo). SerpAPI is optional."""
        from local_agent.tools import choose_search

        self.term.say('\n-- Web search --')
        config = load_config(self.home)
        choice = choose_search(config)
        self.term.say(f'Web search: {choice.why}.')
        if config.search == 'serpapi':
            if get_key('serpapi') or await self.yes('Add a serpapi key so the agent can search the web?', False):
                await self.maybe_key('serpapi', 'serpapi key')
                choice = choose_search(config)
        elif config.search:
            self.term.say('Optional: Google results through SerpAPI instead. Set [web] search = "serpapi" in '
                          f'{self.home.config_path} and run `local-agent key set serpapi`.')
        self.summary['search'] = choice.backend or 'off'

    async def telegram_bot(self) -> str | None:
        """The bot's @name if a working token is set."""
        from local_agent.channels.telegram import TelegramBot, TelegramError

        self.term.say('\n-- Telegram (optional) --')
        if not get_key('telegram'):
            if not await self.yes('Talk to the agent from your phone through your own Telegram bot?', False):
                return None
            self.term.say('In Telegram, message @BotFather, send /newbot, pick a name, and copy the token it gives.')
        elif not await self.yes('A Telegram bot token is already in the keychain. Replace it?', False):
            token = get_key('telegram')
            return await self._bot_name(token)
        while token := await self.secret('Bot token'):
            bot = TelegramBot(token, self.telegram_transport)
            try:
                me = await bot.call('getMe')
            except TelegramError as e:
                self.term.say(f'{e} Check the token and try again.')
                continue
            finally:
                await bot.close()
            set_key('telegram', token)
            self.term.say(f'Saved the bot token in the keychain: @{me.get("username", "your bot")}.')
            return f'@{me.get("username", "your bot")}'
        return await self._bot_name(get_key('telegram')) if get_key('telegram') else None

    async def _bot_name(self, token: str) -> str:
        from local_agent.channels.telegram import TelegramBot, TelegramError

        bot = TelegramBot(token, self.telegram_transport)
        try:
            return f'@{(await bot.call("getMe")).get("username", "your bot")}'
        except TelegramError:
            return 'your bot'
        finally:
            await bot.close()

    async def background(self) -> None:
        self.term.say('\n-- Background service --')
        try:
            service = self.service or service_module.service_for(self.home)
        except ServiceError as e:
            self.term.say(str(e))
            return
        self.service = service
        try:
            if service.is_installed():
                if await self.yes('The background service is installed. Restart it so these settings apply?', True):
                    service.restart()
                    self.term.say('Restarted.')
            elif await self.yes('Run Local Agent in the background (it starts at login, restarts if it crashes)?',
                                True):
                service.install()
                self.term.say(f'Installed: {service.path}. Logs: {service.log_path}')
        except ServiceError as e:
            self.term.say(f'{e}\nYou can run it in a terminal instead: local-agent daemon')

    async def pairing(self, bot_name: str) -> None:
        from local_agent.channels.telegram import OWNER_CHAT, OWNER_NAME, TelegramBot, TelegramChannel, new_pairing
        from local_agent.store import SQLiteStore

        store = SQLiteStore(self.home.db_path)
        try:
            if store.get_setting(OWNER_CHAT):
                self.summary['telegram'] = f'{bot_name}, paired with {store.get_setting(OWNER_NAME)}'
                return
            self.summary['telegram'] = f'{bot_name}, not paired yet (local-agent telegram pair)'
            if not await self.yes(f'Pair your Telegram chat with {bot_name} now?', True):
                return
            daemon_polls = service_module.health(load_config(self.home).daemon_port, self.health_transport)
            code = new_pairing(store)['code']
            self.term.say(f'In Telegram, send {bot_name} this message:  /pair {code}')
            bot = None if daemon_polls else TelegramBot(get_key('telegram'), self.telegram_transport)
            # pairing only touches the store, so the channel gets a store and config instead of a whole Runtime
            channel = TelegramChannel(SimpleNamespace(store=store, config=load_config(self.home)), bot) if bot else None
            try:
                for _ in range(PAIRING_CHECKS):
                    if (await self.ask('Press Enter once you\'ve sent it (or type "skip")')).lower() == 'skip':
                        return
                    if await self._paired(store, channel):
                        self.summary['telegram'] = f'{bot_name}, paired with {store.get_setting(OWNER_NAME)}'
                        self.term.say('Paired. The bot answers you, and only you.')
                        return
                    self.term.say('Not paired yet.' + (' If the daemon started before the bot was set up, run '
                                                       '`local-agent daemon restart`.' if daemon_polls else ''))
            finally:
                if bot:
                    await bot.close()
        finally:
            store.close()

    async def _paired(self, store, channel) -> bool:
        from local_agent.channels.telegram import OWNER_CHAT, TelegramError

        if channel is None:  # the running daemon handles /pair; wait for it to record the owner
            for _ in range(DAEMON_PAIRING_WAIT):
                if store.get_setting(OWNER_CHAT):
                    return True
                await asyncio.sleep(1)
            return False
        try:
            return await channel.poll_for_pairing()
        except TelegramError as e:
            self.term.say(str(e))
            return False

    def finish(self) -> None:
        from local_agent.daemon.commands import web_link

        state = self.service.status() if self.service else None
        running = service_module.health(load_config(self.home).daemon_port, self.health_transport)
        self.term.say('\n-- Ready --')
        self.term.say(f'Model:     {self.summary.get("model", "unchanged")}')
        self.term.say(f'Search:    {self.summary.get("search", "unchanged")}')
        self.term.say(f'Telegram:  {self.summary.get("telegram", "off")}')
        self.term.say('Service:   ' + ('running' if state and state.running else
                                       'installed, not running (local-agent daemon start)' if state and state.installed
                                       else 'not installed (local-agent daemon install, or run local-agent daemon)'))
        self.term.say(f'Web UI:    {web_link(self.home)}' + ('' if running or (state and state.running) else
                                                              '  (once the daemon runs)'))
        self.term.say('Next: `local-agent chat` in this terminal, or message your bot. `local-agent doctor` checks '
                      'everything.')

    async def run(self) -> int:
        self.term.say('Local Agent setup. Enter keeps what\'s in [brackets]; Ctrl-D stops (what\'s saved stays).\n')
        try:
            self.workspace()
            try:
                load_config(self.home)
            except ConfigError as e:
                self.term.say(f'{e}\nFix the settings file, then run `local-agent onboard` again.')
                return 1
            await self.model()
            await self.search()
            bot_name = await self.telegram_bot()
            await self.background()
            if bot_name:
                await self.pairing(bot_name)
            self.finish()
        except Stopped:
            self.term.say('\nStopped. What was saved stays saved; run `local-agent onboard` again any time.')
            return 1
        return 0


def main(home: Home) -> int:
    from local_agent.channels.terminal import Terminal

    try:
        return asyncio.run(Wizard(home, Terminal()).run())
    except KeyboardInterrupt:
        print('\nStopped.', file=sys.stderr)
        return 1
