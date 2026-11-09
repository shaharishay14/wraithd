"""
Where wraithd keeps its state, and the owner's settings.

Everything lives in one folder, ~/.wraithd/ (or $WRAITHD_HOME; an old ~/.local-agent is moved there, rename.py):

    config.toml     model, budgets, owner, web search, voice notes, shell (no secrets: those are in the OS keychain)
    state.db        the event log, conversations, approvals
    workspace/      AGENTS.md (standing orders), memory/, skills/, files/

API keys go in the keychain through `keyring`, never in the config file, so the folder can be backed up or
shared without leaking them.
"""

import json
import logging
import os
import re
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import keyring
import keyring.errors

from wraithd import rename
from wraithd.core import secrets
from wraithd.mcp.config import McpConfigError, parse_servers
from wraithd.store import modes

APP = 'wraithd'  # the keychain service name
HOME_ENV = 'WRAITHD_HOME'

PROVIDERS = ('openai', 'anthropic', 'ollama', 'openai-compatible')
# [web] search: auto is the model provider's own search (openai, anthropic) or duckduckgo for local models
SEARCH_BACKENDS = ('auto', 'openai', 'anthropic', 'duckduckgo', 'serpapi', 'claude')
# [voice] transcribe: auto is openai if its key is set, else local whisper if installed, else off
TRANSCRIBERS = ('auto', 'openai', 'whisper', 'elevenlabs')
# [telegram.commands]: the owner's quick actions, "/news" in the paired chat sends "run news-feed" (the "/" menu).
# Setting the table replaces these. /pair, /mcp_add and /start keep their meaning and can't be taken.
TELEGRAM_COMMANDS = (('news', 'run news-feed'), ('drafts', 'slabbed drafts'), ('jobs', 'find jobs'),
                     ('watchlist', 'show my watch list'), ('review', 'weekly review'))
TELEGRAM_RESERVED = ('pair', 'mcp_add', 'start')
TELEGRAM_COMMAND_NAME = re.compile(r'^[a-z0-9_]{1,32}$')  # what Telegram takes as a bot command
# Where a key comes from when it isn't in the keychain (and the variable is set)
KEY_ENV = {'openai': 'OPENAI_API_KEY', 'anthropic': 'ANTHROPIC_API_KEY', 'serpapi': 'SERPAPI_KEY',
           'telegram': 'TELEGRAM_BOT_TOKEN', 'elevenlabs': 'ELEVENLABS_API_KEY'}
# what `wraithd key set` takes
KEY_NAMES = tuple(p for p in PROVIDERS if p != 'ollama') + ('serpapi', 'telegram', 'elevenlabs')

DEFAULT_CONFIG = """\
# wraithd settings. API keys are not stored here: set them with `wraithd key set <provider>`.

[model]
provider = "openai"          # openai | anthropic | ollama | openai-compatible
default = "gpt-4o-mini"      # used for every run
strong = "gpt-4o"            # used when a skill says `model: strong` (subtle rules); leave empty to never switch
# base_url = "http://localhost:11434/v1"   # ollama and openai-compatible endpoints

[budget]
run_tokens = 200000          # per run, input + output (claude-code: cache reads count a tenth, docs/tokens.md)
run_tool_calls = 40          # per run
# daily_tokens = 2000000     # across the day's runs, weighted as run_tokens; off unless set (claude-code: 2000000)

[owner]
# email = "you@example.com"  # recipients matching this count as written by you
# timezone = "Europe/London" # for schedules; defaults to this computer's timezone

[web]
search = "auto"              # your model provider's own search, on your model key; DuckDuckGo for local models
                             # or openai | anthropic | duckduckgo | serpapi (needs a key) | "" (off)
                             # | claude (your Claude subscription, through Claude Code's web search)
# read = false               # the agent can't open web pages (read_website); true is the default

[voice]
transcribe = "auto"          # Telegram voice notes: openai if its key is set, else local whisper if installed
                             # or openai | whisper | elevenlabs (needs a key) | "" (off)

[shell]
enabled = false              # true: the agent may run shell commands, each one only after you say yes
timeout_seconds = 30

[engine]
name = "agents-sdk"          # agents-sdk: your API key or local model | claude-code: your Claude Pro/Max login
# fallback = false           # claude-code only: on a plan limit before anything ran, use the [model] provider
# chat_model = "sonnet"      # claude-code only: the model for chats
# background_model = "haiku" # claude-code only: heartbeat, schedules, memory and summaries (cheap, saves your plan)
# quick_model = "haiku"      # claude-code only: short chat messages ("": every chat on chat_model). A message
                             # starting with "think" or "/sonnet" uses chat_model (docs/engines.md)
# schedule_model = ""        # claude-code only: scheduled runs ("": background_model). A `model: strong` skill named
                             # in a schedule's message runs on chat_model either way
# idle_minutes = 10          # claude-code only: close a conversation's Claude Code session after this long unused
# max_turns = 8              # claude-code only: model calls per run; then it stops and says what was done

[daemon]
port = 7878                  # the API and web UI, on 127.0.0.1 only
# dev_origin = "http://localhost:5173"   # a frontend dev server, while developing the UI
"""



@dataclass(frozen=True)
class Section:
    """
    One module's own settings: a [table] in config.toml, merged over these defaults. A value must have its default's
    type (a default of None takes any value). check(values) may raise ConfigError for anything else to validate.
    """

    defaults: dict
    check: Callable[[dict], None] | None = None
    open: bool = False  # any key is kept (e.g. [watch.<name>] tables, one per name); check() validates them


# The tables the core reads itself; a module's section can't take one of these names
CORE_TABLES = ('model', 'budget', 'owner', 'web', 'voice', 'shell', 'daemon', 'telegram', 'mcp')

# Each module's settings, by table name. Read them with config.section(name). Each worker inside its block only.
SECTIONS: dict[str, Section] = {
    # >>> security
    # <<< security
    # >>> efficiency
    # A purpose's own model name (of the [model] provider); "" uses [model] default, as before
    'models': Section({'distill': '', 'compaction': '', 'heartbeat': '', 'subagent': ''}),
    # Tool output over over_chars goes to workspace/files/.spill/ (0 or less: off); kept keep_days (0 or less: kept)
    'spill': Section({'over_chars': 4000, 'keep_days': 7}),
    # <<< efficiency
    # >>> capabilities
    'browser': Section({'enabled': False, 'timeout_seconds': 20, 'idle_seconds': 60}),
    'subagents': Section({'enabled': False, 'default_tokens': 50_000}),
    'heartbeat': Section({'enabled': False, 'every_minutes': 30, 'quiet_hours': '22:00-07:00', 'max_tokens': 20_000},
                         lambda values: __import__('wraithd.daemon.heartbeat', fromlist=['_']).check(values)),
    # [watch.<name>] tables: feeds polled in code, the agent runs only on new items (daemon/watch.py)
    'watch': Section({}, lambda values: __import__('wraithd.daemon.watch', fromlist=['_']).check(values), open=True),
    # <<< capabilities
    # >>> reach
    'remote': Section({'allowed_hosts': []},
                      lambda values: __import__('wraithd.daemon.security', fromlist=['_']).check_remote(values)),
    # <<< reach
    # >>> evals
    # <<< evals
    # >>> sandbox
    # The shell's (and opted-in MCP servers') sandbox: none | docker. Broken values load, and the audit names them
    'sandbox': Section({'backend': 'none', 'image': 'debian:bookworm-slim', 'network': 'none'}),
    # <<< sandbox
    # Who thinks: the Agents SDK on [model] (agents-sdk) or Claude Code on the owner's own login (claude-code)
    'engine': Section({'name': 'agents-sdk', 'chat_model': 'sonnet', 'background_model': 'haiku',
                       'schedule_model': '', 'quick_model': 'haiku', 'fallback': False, 'idle_minutes': 10,
                       'max_turns': 8},
                      lambda values: _check_engine(values)),
}

ENGINES = ('agents-sdk', 'claude-code')
# The daily token budget in subscription mode when [budget] daily_tokens isn't set: the owner's own Claude chats
# share the plan, so background work must not use it all up
SUBSCRIPTION_DAILY_TOKENS = 2_000_000


def _check_engine(values: dict) -> None:
    if values['name'] not in ENGINES:
        raise ConfigError(f'[engine] name must be one of {", ".join(ENGINES)}, not {values["name"]!r}.')
    for key in ('chat_model', 'background_model'):
        if not values[key].strip():
            raise ConfigError(f'[engine] {key} must name a model, e.g. "sonnet" or "haiku".')
    if values['idle_minutes'] <= 0:
        raise ConfigError('[engine] idle_minutes must be a positive whole number.')
    if values['max_turns'] <= 0:
        raise ConfigError('[engine] max_turns must be a positive whole number.')


AGENTS_MD = """\
# Standing orders

Anything here is in every run. Write it like a note to an assistant: who you are, how you like answers,
what to never do. wraithd treats it as your own words.
"""


@dataclass(frozen=True)
class ModelConfig:
    provider: str = 'openai'
    default: str = 'gpt-4o-mini'
    strong: str = 'gpt-4o'
    base_url: str = ''


@dataclass(frozen=True)
class Config:
    model: ModelConfig = field(default_factory=ModelConfig)
    run_tokens: int | None = 200_000
    run_tool_calls: int | None = 40
    daily_tokens: int | None = None
    owner_email: str = ''
    timezone: str = ''  # IANA name; empty: the system's (schedules.local_timezone)
    search: str = 'auto'  # a SEARCH_BACKENDS name; '' turns web search off
    web_read: bool = True  # [web] read: false takes read_website away from every run
    transcribe: str = 'auto'  # a TRANSCRIBERS name; '' turns voice notes off
    shell_enabled: bool = False
    shell_timeout: int = 30
    mcp_servers: tuple = ()  # mcp.config.McpServer, from the [mcp.servers.*] blocks
    mcp_recent_actions: int = 10  # [mcp] recent_actions: app actions used last week, offered directly (0: off)
    telegram_commands: tuple = TELEGRAM_COMMANDS  # (name, message) pairs, from [telegram.commands]
    daemon_port: int = 7878
    dev_origin: str = ''
    sections: dict = field(default_factory=dict)  # table name -> values, for SECTIONS

    def section(self, name: str) -> dict:
        """A module's settings (SECTIONS[name]): what config.toml sets, over the defaults."""
        return {**SECTIONS[name].defaults, **self.sections.get(name, {})}


class ConfigError(Exception):
    """The config file can't be used. The message says what to fix."""


@dataclass(frozen=True)
class Home:
    """The state folder and the paths inside it."""

    root: Path

    @classmethod
    def locate(cls) -> 'Home':
        # an old ~/.local-agent is moved here the first time (rename.py)
        return cls(rename.locate(os.environ.get(HOME_ENV), Path.home() / '.wraithd'))

    config_path = property(lambda self: self.root / 'config.toml')
    db_path = property(lambda self: self.root / 'state.db')
    workspace = property(lambda self: self.root / 'workspace')
    agents_md = property(lambda self: self.workspace / 'AGENTS.md')
    memory_dir = property(lambda self: self.workspace / 'memory')
    skills_dir = property(lambda self: self.workspace / 'skills')
    files_dir = property(lambda self: self.workspace / 'files')

    def ensure(self) -> None:
        """Creates the folder layout and a default config on first use. Never overwrites anything."""
        # Private whatever the umask (Ubuntu's 002 would make them group-writable): store/modes.py
        modes.tighten_umask()
        for folder in (self.root, self.memory_dir, self.skills_dir, self.files_dir):
            modes.make_dirs(folder)
        self.root.chmod(0o700)  # memory and the event log are private: only the owner's account can read them
        modes.repair(self.root)  # a home made under umask 002 before: no group/other write on anything in it
        if not self.config_path.exists():
            modes.write_private(self.config_path, DEFAULT_CONFIG)
        if not self.agents_md.exists():
            modes.write_private(self.agents_md, AGENTS_MD)


_warned: set[str] = set()


def _warn_once(message: str) -> None:
    """A setting that no longer does anything: said once per process, not on every load."""
    if message not in _warned:
        _warned.add(message)
        logging.getLogger(__name__).warning(message)


def _positive_or_none(value, name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f'{name} must be a positive whole number (or left out for the default).')
    return value


def _same_type(value, default) -> bool:
    if default is None:
        return True
    if isinstance(default, bool) or isinstance(value, bool):
        return isinstance(value, bool) and isinstance(default, bool)
    if isinstance(default, float):
        return isinstance(value, int | float)
    return isinstance(value, type(default))


def load_sections(data: dict, sections: dict[str, Section] | None = None) -> dict[str, dict]:
    """Each module's [table], merged over its defaults and checked. Unknown keys are ignored, with a warning."""
    found = {}
    for name, section in (SECTIONS if sections is None else sections).items():
        if name in CORE_TABLES:
            raise ConfigError(f'[{name}] is a core settings table; a module needs a table of its own.')
        table = data.get(name, {})
        if not isinstance(table, dict):
            raise ConfigError(f'[{name}] must be a table of settings.')
        for key in () if section.open else table.keys() - section.defaults.keys():
            _warn_once(f'[{name}] {key} is not a setting; it is ignored.')
        values = {**section.defaults,
                  **{key: value for key, value in table.items() if section.open or key in section.defaults}}
        for key, value in values.items():
            default = section.defaults.get(key)
            if not _same_type(value, default):
                raise ConfigError(f'[{name}] {key} must be {type(default).__name__}, not {value!r}.')
        if section.check:
            section.check(values)
        found[name] = values
    return found


def load_config(home: Home) -> Config:
    """The owner's config, with defaults for anything not set."""
    try:
        data = tomllib.loads(home.config_path.read_text()) if home.config_path.exists() else {}
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f'{home.config_path} is not valid TOML: {e}') from None
    model, budget, owner = data.get('model', {}), data.get('budget', {}), data.get('owner', {})
    web, shell, daemon = data.get('web', {}), data.get('shell', {}), data.get('daemon', {})
    telegram, voice = data.get('telegram', {}), data.get('voice', {})
    port = daemon.get('port', Config.daemon_port)
    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
        raise ConfigError('[daemon] port must be a number from 1024 to 65535.')
    dev_origin = daemon.get('dev_origin', '')
    if dev_origin and not (isinstance(dev_origin, str) and dev_origin.startswith(('http://localhost:',
                                                                                   'http://127.0.0.1:'))):
        raise ConfigError('[daemon] dev_origin must be a local dev server, e.g. "http://localhost:5173".')
    provider = model.get('provider', ModelConfig.provider)
    if provider not in PROVIDERS:
        raise ConfigError(f'[model] provider must be one of {", ".join(PROVIDERS)}, not {provider!r}.')
    if provider == 'openai-compatible' and not model.get('base_url'):
        raise ConfigError('[model] base_url is required for an openai-compatible endpoint.')
    search = web.get('search', Config.search) or ''
    if search and search not in SEARCH_BACKENDS:
        raise ConfigError(f'[web] search must be one of {", ".join(SEARCH_BACKENDS)}, or "" for none.')
    transcribe = voice.get('transcribe', Config.transcribe) or ''
    if transcribe and transcribe not in TRANSCRIBERS:
        raise ConfigError(f'[voice] transcribe must be one of {", ".join(TRANSCRIBERS)}, or "" for none.')
    if not isinstance(web.get('read', True), bool):
        raise ConfigError('[web] read must be true or false.')
    if not isinstance(shell.get('enabled', False), bool):
        raise ConfigError('[shell] enabled must be true or false.')
    if 'answer_strangers' in telegram:
        _warn_once('[telegram] answer_strangers is gone: the bot only ever answers its paired owner. '
                   f'You can delete that line from {home.config_path}.')
    telegram_commands = _telegram_commands(telegram.get('commands', None))
    timezone = owner.get('timezone', '')
    if timezone:
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ConfigError(f'[owner] timezone {timezone!r} is not a timezone name, e.g. "Europe/London".') from None
    try:
        mcp_servers = parse_servers(data)
    except McpConfigError as e:
        raise ConfigError(str(e)) from None
    recent_actions = (data.get('mcp') or {}).get('recent_actions', Config.mcp_recent_actions)
    if not isinstance(recent_actions, int) or isinstance(recent_actions, bool) or recent_actions < 0:
        raise ConfigError('[mcp] recent_actions must be a number, 0 or more (0 turns it off).')
    sections = load_sections(data)
    daily_tokens = _positive_or_none(budget.get('daily_tokens'), 'daily_tokens')
    if 'daily_tokens' not in budget and sections.get('engine', {}).get('name') == 'claude-code':
        daily_tokens = SUBSCRIPTION_DAILY_TOKENS  # on by default in subscription mode
    return Config(
        sections=sections,
        mcp_servers=mcp_servers,
        mcp_recent_actions=recent_actions,
        telegram_commands=telegram_commands,
        daemon_port=port,
        dev_origin=dev_origin,
        model=ModelConfig(provider=provider, default=model.get('default', ModelConfig.default),
                          strong=model.get('strong', ModelConfig.strong), base_url=model.get('base_url', '')),
        run_tokens=_positive_or_none(budget.get('run_tokens', Config.run_tokens), 'run_tokens'),
        run_tool_calls=_positive_or_none(budget.get('run_tool_calls', Config.run_tool_calls), 'run_tool_calls'),
        daily_tokens=daily_tokens,
        owner_email=owner.get('email', ''),
        timezone=timezone,
        search=search,
        web_read=web.get('read', True),
        transcribe=transcribe,
        shell_enabled=shell.get('enabled', False),
        shell_timeout=_positive_or_none(shell.get('timeout_seconds', Config.shell_timeout), 'timeout_seconds')
        or Config.shell_timeout,
    )


def _telegram_commands(table) -> tuple:
    """[telegram.commands] as (name, message) pairs; the defaults when it isn't set. A name Telegram won't take, a
    reserved one or an empty message is left out with a warning, so the rest still work."""
    if table is None:
        return TELEGRAM_COMMANDS
    if not isinstance(table, dict):
        _warn_once('[telegram.commands] must be a table of name = "message" lines; using the defaults.')
        return TELEGRAM_COMMANDS
    commands = []
    for name, message in table.items():
        if not TELEGRAM_COMMAND_NAME.match(str(name)):
            _warn_once(f'[telegram.commands] {name!r} is not a Telegram command name (a-z, 0-9 and _, at most 32); '
                       'it is ignored.')
        elif name in TELEGRAM_RESERVED:
            _warn_once(f'[telegram.commands] /{name} keeps its own meaning; that line is ignored.')
        elif not isinstance(message, str) or not message.strip():
            _warn_once(f'[telegram.commands] {name} needs a message, e.g. {name} = "run news-feed"; it is ignored.')
        else:
            commands.append((name, ' '.join(message.split())))
    return tuple(commands)


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return str(value) if isinstance(value, int) else json.dumps(value)  # a JSON string is a TOML basic string


def set_values(home: Home, section: str, values: dict) -> None:
    """
    Sets keys in one [section] of config.toml (onboard's answers), keeping the owner's other lines and comments.
    A key set in the section is replaced in place, keeping its trailing comment; a commented-out example
    (`# base_url = ...`) is replaced by the real line; anything else is added at the end of the section. The
    result must load, or the file is left as it was.
    """
    home.ensure()
    before = home.config_path.read_text()
    lines = before.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == f'[{section}]'), None)
    if start is None:
        lines += ['', f'[{section}]']
        start = len(lines) - 1
    for key, value in values.items():
        end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith('[')), len(lines))
        rendered = f'{key} = {_toml_value(value)}'
        live = re.compile(rf'^\s*{re.escape(key)}\s*=\s*("(?:[^"\\]|\\.)*"|[^#\s]*)(\s*#.*)?$')
        example = re.compile(rf'^\s*#\s*{re.escape(key)}\s*=')
        found = next((i for i in range(start + 1, end) if live.match(lines[i])), None)
        if found is not None:
            comment = live.match(lines[found]).group(2) or ''
            if comment:  # keep the comment where it was, when the new value fits before it
                width = len(lines[found]) - len(comment.lstrip()) - 1
                rendered = rendered.ljust(width) + ' ' + comment.lstrip()
            lines[found] = rendered
            continue
        found = next((i for i in range(start + 1, end) if example.match(lines[i])), None)
        if found is not None:
            lines[found] = rendered
            continue
        last = end
        while last - 1 > start and not lines[last - 1].strip():
            last -= 1
        lines.insert(last, rendered)
    handle, temporary = tempfile.mkstemp(dir=home.root, prefix='.config.', suffix='.tmp')
    with os.fdopen(handle, 'w') as out:
        out.write('\n'.join(lines) + '\n')
    os.replace(temporary, home.config_path)
    try:
        load_config(home)
    except ConfigError:
        home.config_path.write_text(before)
        raise


# --- keys --------------------------------------------------------------------------------------------------

class NoKeychain(ConfigError):
    """This machine has no keychain service (a headless Linux server): keys come from the environment instead."""


def no_keychain_message(provider: str) -> str:
    variable = KEY_ENV.get(provider, f'the {provider} variable')
    return (f'This machine has no keychain, so the key can\'t be saved. Give it to wraithd as the environment '
            f'variable {variable} instead (an env file, a systemd EnvironmentFile, or a password manager\'s '
            f'`run` command): see docs/keys.md.')


def keychain_get(account: str) -> str | None:
    """The keychain's value, or None when it has none or the machine has no keychain at all."""
    try:
        value = keyring.get_password(APP, account)
    except keyring.errors.NoKeyringError:
        return None
    return value or rename.old_keychain_get(account)


def set_key(provider: str, value: str) -> None:
    try:
        keyring.set_password(APP, provider, value)
    except keyring.errors.NoKeyringError as e:
        raise NoKeychain(no_keychain_message(provider)) from e


def delete_key(provider: str) -> bool:
    """Removes the provider's key from the keychain (and any left under the old name). True if one was there.
    A key in an environment variable stays: wraithd didn't set it."""
    found = False
    for service in (APP, rename.OLD_APP):
        try:
            if keyring.get_password(service, provider) is not None:
                keyring.delete_password(service, provider)
                found = True
        except (keyring.errors.NoKeyringError, keyring.errors.PasswordDeleteError):
            pass
    return found


def get_key(provider: str) -> str | None:
    """The provider's key from systemd's credentials (secrets.credential: a file named like its environment variable,
    or like the provider), else the keychain, else its environment variable. Registered for redaction."""
    variable = KEY_ENV.get(provider, '')
    value = secrets.credential(variable, provider) or keychain_get(provider) or os.environ.get(variable, '') or None
    if value:
        secrets.register_secret(value)  # a key the process holds must never reach the model or the log
    return value
