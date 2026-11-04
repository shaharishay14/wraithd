"""
`wraithd onboard`: from nothing to a working agent, one question at a time.

    workspace -> how wraithd thinks (Claude subscription, or a model and key, or a local model) -> web search
    -> Telegram bot -> background service -> Telegram pairing -> a summary with the web UI link

On a real terminal every choice is a picker (arrow keys, Enter, or type) and it opens with the wraithd icon;
in plain mode (pipes, NO_COLOR, --plain: wraithd.tui) the questions are typed, exactly as they always were.

Safe to run again: every question defaults to what's already set (Enter keeps it), a key already in the keychain
is only replaced when the owner says so, and the config file keeps its comments (config.set_values). Ctrl-D stops
the wizard; what was saved before that stays saved.

The key check makes one small request to the provider (its model list, not a model call). It's asked for, can be
skipped, and is passed in as `check_key` so tests never make it.

The Claude subscription never asks for a key or a token (docs/claude-subscription.md): the owner signs in to Claude
Code themselves (`claude`, then /login), and wraithd only checks which credential Claude Code chose (doctor's
claude_login_check, passed in as `check_login` so tests never start Claude Code).
"""

import asyncio
import importlib.util
import shutil
import sys
from dataclasses import dataclass, field
from types import SimpleNamespace

import httpx

from wraithd.config import (
    PROVIDERS,
    ConfigError,
    Home,
    NoKeychain,
    get_key,
    load_config,
    set_key,
    set_values,
)
from wraithd.daemon import service as service_module
from wraithd.daemon.service import ServiceError
from wraithd.doctor import CLAUDE_ENGINE, CLAUDE_EXTRA, DEFAULT_ENGINE, engine_name
from wraithd.providers import OLLAMA_URL

# What a new provider starts with; the owner can type any model name instead
MODELS = {'openai': ('gpt-4o-mini', 'gpt-4o'), 'anthropic': ('claude-haiku-5-5', 'claude-sonnet-5-5'),
          'ollama': ('llama3.2', ''), 'openai-compatible': ('', '')}
# What the model picker offers (with "other..." for any name); the owner's current model is always offered too
KNOWN_MODELS = {'openai': ('gpt-4o-mini', 'gpt-4o', 'gpt-4.1-mini', 'gpt-4.1'),
                'anthropic': ('claude-haiku-5-5', 'claude-sonnet-5-5', 'claude-opus-5-5'),
                'ollama': ('llama3.2', 'qwen2.5:7b'), 'openai-compatible': ()}
OTHER = 'other…'
ENGINE_HINTS = {'subscription': 'your Claude Pro or Max plan, through Claude Code (no API key)',
                'api-key': 'an OpenAI or Anthropic key (or another OpenAI-compatible endpoint)',
                'local': 'a model on this machine, through Ollama'}
NEEDS_KEY = {'openai', 'anthropic'}  # ollama needs none; a self-hosted endpoint may or may not
PAIRING_CHECKS = 3  # how many times the owner can press Enter before pairing is left for later
DAEMON_PAIRING_WAIT = 15  # seconds to wait for a running daemon to see the /pair message
# How wraithd thinks: Claude Code on the owner's own Claude login, a provider's API key, or a local model
SUBSCRIPTION, API_KEY, LOCAL = 'subscription', 'api-key', 'local'
ENGINES = (SUBSCRIPTION, API_KEY, LOCAL)
KEY_PROVIDERS = tuple(p for p in PROVIDERS if p != 'ollama')
LOGIN_CHECKS = 3  # how many times the owner can press Enter before the login is left for later


class Stopped(Exception):
    """The owner ended input (Ctrl-D)."""


def claude_installed() -> tuple[bool, bool]:
    """(the Claude Agent SDK is importable, a `claude` command is on PATH). Looks, doesn't import or start it."""
    return importlib.util.find_spec('claude_agent_sdk') is not None, shutil.which('claude') is not None


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
    check_login: object = None  # async (home) -> (ok, message); None: doctor.claude_login_check
    claude_installed: object = claude_installed
    telegram_transport: httpx.AsyncBaseTransport | None = None
    health_transport: httpx.BaseTransport | None = None
    summary: dict = field(default_factory=dict)

    # --- asking ----------------------------------------------------------------------------------------------
    # On a rich terminal (channels.terminal.RichTerminal) choices are pickers; the plain one types them

    @property
    def rich(self) -> bool:
        return getattr(self.term, 'rich', False)

    async def picked(self, question: str, options, default: str | None, free: bool = False) -> str:
        answer = await self.term.pick(question, options, default, free=free)
        if answer is None:
            raise Stopped
        return answer

    def section(self, title: str) -> None:
        if self.rich:
            self.term.section(title)
        else:
            self.term.say(f'\n-- {title} --')

    async def ready(self, question: str, picker: str, ready: str) -> bool:
        """'Press Enter once you've ... (or type "skip")', or the picker's question: True to check now."""
        if self.rich:
            from wraithd.tui.pick import Option

            return await self.picked(picker, [Option('check', ready), Option('skip', 'skip for now')],
                                     'check') == 'check'
        return (await self.ask(question)).lower() != 'skip'

    async def ask(self, question: str, default: str = '') -> str:
        answer = await self.term.ask(f'{question}{f" [{default}]" if default else ""}: ')
        if answer is None:
            raise Stopped
        return answer.strip() or default

    async def yes(self, question: str, default: bool) -> bool:
        if self.rich:
            return await self.picked(question, ('yes', 'no'), 'yes' if default else 'no') == 'yes'
        while True:
            answer = (await self.ask(f'{question} {"[Y/n]" if default else "[y/N]"}')).lower()
            if not answer:
                return default
            if answer in ('y', 'yes', 'n', 'no'):
                return answer.startswith('y')
            self.term.say('Please answer y or n.')

    async def choose(self, question: str, choices, default: str, hints: dict | None = None) -> str:
        if self.rich:
            from wraithd.tui.pick import Option

            return await self.picked(question, [Option(c, hint=(hints or {}).get(c, '')) for c in choices], default)
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
        if get_key(name) and not await self.yes(f'{what} is already set. Replace it?', False):
            return True
        value = await self.secret(what)
        if value and self.save_key(name, value):
            self.term.say(f'Saved the {name} key in the keychain.')
        return bool(get_key(name))

    def save_key(self, name: str, value: str) -> bool:
        """Saves a key, or says how to give it through the environment when this machine has no keychain."""
        try:
            set_key(name, value)
        except NoKeychain as e:
            self.term.say(str(e))
            return False
        return True

    # --- the steps -------------------------------------------------------------------------------------------

    def workspace(self) -> None:
        existed = self.home.workspace.exists()
        self.home.ensure()
        self.term.say(f'Workspace: {self.home.workspace} ({"already there" if existed else "created"})')
        self.term.say(f'Settings: {self.home.config_path}')

    async def thinking(self) -> None:
        """How wraithd thinks: the Claude subscription, an API key, or a local model. Asked again if the
        subscription can't be set up here yet."""
        self.section('How wraithd thinks')
        if not self.rich:  # the picker shows these next to each choice
            self.term.say('subscription: your Claude Pro or Max plan, through Claude Code (no API key)\n'
                      'api-key: an OpenAI or Anthropic key (or another OpenAI-compatible endpoint)\n'
                      'local: a model on this machine, through Ollama')
        current = load_config(self.home).model
        engine = engine_name(self.home)
        default = SUBSCRIPTION if engine == CLAUDE_ENGINE else LOCAL if current.provider == 'ollama' else API_KEY
        while True:
            choice = await self.choose('How should wraithd think?', ENGINES, default, ENGINE_HINTS)
            if choice == SUBSCRIPTION:
                if await self.subscription():
                    return
                default = API_KEY
                self.term.say('Pick another way for now (you can switch later with `wraithd onboard`), or '
                              'press Ctrl-D and come back.')
                continue
            if engine != DEFAULT_ENGINE:  # back from Claude Code to the provider's engine
                set_values(self.home, 'engine', {'name': DEFAULT_ENGINE})
            await self.model(local=choice == LOCAL)
            return

    async def subscription(self) -> bool:
        """Claude Code on the owner's own login. Never asks for a key or token. True once [engine] is set."""
        sdk, cli = self.claude_installed()
        if not sdk:
            self.term.say(f'Claude Code isn\'t installed with wraithd yet. Install it with: {CLAUDE_EXTRA}\n'
                          '(or pip install \'wraithd[claude]\'), then run `wraithd onboard` again.')
            return False
        self.term.say('Sign in to Claude Code yourself; wraithd never sees the login. In another terminal:\n'
                      '  claude\n'
                      'then type /login and sign in with your Claude account. On a machine with no browser, it '
                      'prints a link: open it on any device and paste the code back.')
        if not cli:
            self.term.say('No `claude` command here yet: install Claude Code (https://claude.com/claude-code) '
                          'to sign in. wraithd runs the copy that came with it.')
        check = self.check_login
        if check is None:
            from wraithd.doctor import claude_login_check as check
        for _ in range(LOGIN_CHECKS):
            if not await self.ready('Press Enter once you\'ve signed in (or type "skip")', 'Signed in?',
                                    'I\'ve signed in: check'):
                break
            ok, message = await check(self.home)
            if ok:
                set_values(self.home, 'engine', {'name': CLAUDE_ENGINE})
                self.term.say(f'Check: {message}. wraithd will think with your Claude plan.')
                self.summary['model'] = 'Claude subscription (Claude Code, your own login)'
                return True
            self.term.say(f'Not yet: {message}.')
        self.term.say('The login isn\'t checked yet, so wraithd stays as it was. Once you\'ve signed in, run '
                      '`wraithd onboard` again (or `wraithd doctor` to see what Claude Code uses).')
        return False

    async def model(self, local: bool = False) -> None:
        self.section('Model')
        current = load_config(self.home).model
        if local:
            provider = 'ollama'
        else:
            provider = await self.choose('Provider', KEY_PROVIDERS,
                                         current.provider if current.provider in KEY_PROVIDERS else 'openai')
        default, strong = (current.default, current.strong) if provider == current.provider else MODELS[provider]
        values = {'provider': provider}
        if provider in ('ollama', 'openai-compatible'):
            base_url = current.base_url or (OLLAMA_URL if provider == 'ollama' else '')
            while not (base_url := await self.ask('Endpoint URL', base_url)):
                self.term.say('An openai-compatible endpoint needs its URL, e.g. http://localhost:8000/v1.')
            values['base_url'] = base_url
        while not (default := await self.model_name(provider, 'Model for every run', default)):
            self.term.say('Type the model\'s name, as the endpoint knows it.')
        values['default'] = default
        answer = await self.model_name(provider, 'Stronger model, for skills that need one ("none" for none)',
                                       strong or 'none', none=True)
        values['strong'] = '' if answer == 'none' else answer
        set_values(self.home, 'model', values)
        self.summary['model'] = f'{provider}: {default}' + (f' (strong: {values["strong"]})' if values['strong']
                                                            else '')

        has_key = True
        if provider in NEEDS_KEY or provider == 'openai-compatible':
            what = f'{provider} API key' + (' (if the endpoint wants one)' if provider not in NEEDS_KEY else '')
            has_key = await self.maybe_key(provider, what)
            if not has_key and provider in NEEDS_KEY:
                self.term.say(f'No key yet: runs won\'t work until you set one (wraithd key set {provider}).')
        if has_key and await self.yes('Check it with one small request to the provider?', True):
            ok, message = self.check_key(provider, get_key(provider), values.get('base_url', ''))
            self.term.say(f'Check: {message}.' if ok else f'Check failed: {message}. You can fix it later with '
                                                          f'`wraithd key set {provider}` and `wraithd doctor`.')

    async def model_name(self, provider: str, question: str, default: str, none: bool = False) -> str:
        """A model's name: typed in plain mode; picked from the known ones (or "other…", or typed) on a terminal."""
        known = [name for name in dict.fromkeys((default, *KNOWN_MODELS.get(provider, ()))) if name and name != 'none']
        if not self.rich or not known:
            return await self.ask(question, default)
        from wraithd.tui.pick import Option

        options = [Option(name) for name in known] + ([Option('none', hint='no stronger model')] if none else [])
        options.append(Option(OTHER, hint='type its name'))
        answer = await self.picked(question.split(' (')[0], options, default, free=True)
        if answer == OTHER:
            return await self.ask('Model name')
        return answer

    async def search(self) -> None:
        """Nothing to ask by default: search uses the model provider's own (or DuckDuckGo). SerpAPI is optional."""
        from wraithd.tools import choose_search

        self.section('Web search')
        config = load_config(self.home)
        choice = choose_search(config)
        self.term.say(f'Web search: {choice.why}.')
        if config.search == 'serpapi':
            if get_key('serpapi') or await self.yes('Add a serpapi key so the agent can search the web?', False):
                await self.maybe_key('serpapi', 'serpapi key')
                choice = choose_search(config)
        elif config.search:
            self.term.say('Optional: Google results through SerpAPI instead. Set [web] search = "serpapi" in '
                          f'{self.home.config_path} and run `wraithd key set serpapi`.')
        self.summary['search'] = choice.backend or 'off'

    async def telegram_bot(self) -> str | None:
        """The bot's @name if a working token is set."""
        from wraithd.channels.telegram import TelegramBot, TelegramError

        self.section('Telegram (optional)')
        if not get_key('telegram'):
            if not await self.yes('Talk to the agent from your phone through your own Telegram bot?', False):
                return None
            self.term.say('In Telegram, message @BotFather, send /newbot, pick a name, and copy the token it gives.')
        elif not await self.yes('A Telegram bot token is already set. Replace it?', False):
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
            if not self.save_key('telegram', token):
                return None
            self.term.say(f'Saved the bot token in the keychain: @{me.get("username", "your bot")}.')
            return f'@{me.get("username", "your bot")}'
        return await self._bot_name(get_key('telegram')) if get_key('telegram') else None

    async def _bot_name(self, token: str) -> str:
        from wraithd.channels.telegram import TelegramBot, TelegramError

        bot = TelegramBot(token, self.telegram_transport)
        try:
            return f'@{(await bot.call("getMe")).get("username", "your bot")}'
        except TelegramError:
            return 'your bot'
        finally:
            await bot.close()

    async def background(self) -> None:
        self.section('Background service')
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
            elif await self.yes('Run wraithd in the background (it starts at login, restarts if it crashes)?',
                                True):
                service.install()
                self.home = service.home  # moved from ~/.local-agent, if the old service was there (rename.py)
                self.term.say(f'Installed: {service.path}. Logs: {service.log_path}')
        except ServiceError as e:
            self.term.say(f'{e}\nYou can run it in a terminal instead: wraithd daemon')

    async def pairing(self, bot_name: str) -> None:
        from wraithd.channels.telegram import OWNER_CHAT, OWNER_NAME, TelegramBot, TelegramChannel, new_pairing
        from wraithd.store import SQLiteStore

        store = SQLiteStore(self.home.db_path)
        try:
            if store.get_setting(OWNER_CHAT):
                self.summary['telegram'] = f'{bot_name}, paired with {store.get_setting(OWNER_NAME)}'
                return
            self.summary['telegram'] = f'{bot_name}, not paired yet (wraithd telegram pair)'
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
                    if not await self.ready('Press Enter once you\'ve sent it (or type "skip")', 'Sent it?',
                                            'I\'ve sent it: check'):
                        return
                    if await self._paired(store, channel):
                        self.summary['telegram'] = f'{bot_name}, paired with {store.get_setting(OWNER_NAME)}'
                        self.term.say('Paired. The bot answers you, and only you.')
                        return
                    self.term.say('Not paired yet.' + (' If the daemon started before the bot was set up, run '
                                                       '`wraithd daemon restart`.' if daemon_polls else ''))
            finally:
                if bot:
                    await bot.close()
        finally:
            store.close()

    async def _paired(self, store, channel) -> bool:
        from wraithd.channels.telegram import OWNER_CHAT, TelegramError

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
        from wraithd.daemon.commands import web_link

        state = self.service.status() if self.service else None
        running = service_module.health(load_config(self.home).daemon_port, self.health_transport)
        self.section('Ready')
        self.term.say(f'Model:     {self.summary.get("model", "unchanged")}')
        self.term.say(f'Search:    {self.summary.get("search", "unchanged")}')
        self.term.say(f'Telegram:  {self.summary.get("telegram", "off")}')
        self.term.say('Service:   ' + ('running' if state and state.running else
                                       'installed, not running (wraithd daemon start)' if state and state.installed
                                       else 'not installed (wraithd daemon install, or run wraithd daemon)'))
        self.term.say(f'Web UI:    {web_link(self.home)}' + ('' if running or (state and state.running) else
                                                              '  (once the daemon runs)'))
        self.term.say('Next: `wraithd chat` in this terminal, or message your bot. `wraithd doctor` checks '
                      'everything.')

    async def run(self) -> int:
        if self.rich:
            from wraithd.tui import art

            art.draw(self.term.console)
            self.term.show('[dim]Setup. ↑↓ and Enter pick, or type; Enter keeps what\'s set now. Ctrl-D stops '
                           '(what\'s saved stays).[/dim]\n')
        else:
            self.term.say('wraithd setup. Enter keeps what\'s in [brackets]; Ctrl-D stops (what\'s saved stays).\n')
        try:
            self.workspace()
            try:
                load_config(self.home)
            except ConfigError as e:
                self.term.say(f'{e}\nFix the settings file, then run `wraithd onboard` again.')
                return 1
            await self.thinking()
            await self.search()
            bot_name = await self.telegram_bot()
            await self.background()
            if bot_name:
                await self.pairing(bot_name)
            self.finish()
        except Stopped:
            self.term.say('\nStopped. What was saved stays saved; run `wraithd onboard` again any time.')
            return 1
        return 0


def main(home: Home) -> int:
    from wraithd.channels.terminal import terminal_for

    try:
        return asyncio.run(Wizard(home, terminal_for()).run())
    except KeyboardInterrupt:
        print('\nStopped.', file=sys.stderr)
        return 1
