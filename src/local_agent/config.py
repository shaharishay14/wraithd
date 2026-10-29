"""
Where Local Agent keeps its state, and the owner's settings.

Everything lives in one folder, ~/.local-agent/ (or $LOCAL_AGENT_HOME):

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

from local_agent.core import secrets
from local_agent.mcp.config import McpConfigError, parse_servers

APP = 'local-agent'  # the keychain service name
HOME_ENV = 'LOCAL_AGENT_HOME'

PROVIDERS = ('openai', 'anthropic', 'ollama', 'openai-compatible')
# [web] search: auto is the model provider's own search (openai, anthropic) or duckduckgo for local models
SEARCH_BACKENDS = ('auto', 'openai', 'anthropic', 'duckduckgo', 'serpapi')
# [voice] transcribe: auto is openai if its key is set, else local whisper if installed, else off
TRANSCRIBERS = ('auto', 'openai', 'whisper', 'elevenlabs')
# Where a key comes from when it isn't in the keychain (and the variable is set)
KEY_ENV = {'openai': 'OPENAI_API_KEY', 'anthropic': 'ANTHROPIC_API_KEY', 'serpapi': 'SERPAPI_KEY',
           'telegram': 'TELEGRAM_BOT_TOKEN', 'elevenlabs': 'ELEVENLABS_API_KEY'}
# what `local-agent key set` takes
KEY_NAMES = tuple(p for p in PROVIDERS if p != 'ollama') + ('serpapi', 'telegram', 'elevenlabs')

DEFAULT_CONFIG = """\
# Local Agent settings. API keys are not stored here: set them with `local-agent key set <provider>`.

[model]
provider = "openai"          # openai | anthropic | ollama | openai-compatible
default = "gpt-4o-mini"      # used for every run
strong = "gpt-4o"            # used when a skill says `model: strong` (subtle rules); leave empty to never switch
# base_url = "http://localhost:11434/v1"   # ollama and openai-compatible endpoints

[budget]
run_tokens = 200000          # per run, input + output
run_tool_calls = 40          # per run
# daily_tokens = 2000000     # across the day's runs; off unless set

[owner]
# email = "you@example.com"  # recipients matching this count as written by you
# timezone = "Europe/London" # for schedules; defaults to this computer's timezone

[web]
search = "auto"              # your model provider's own search, on your model key; DuckDuckGo for local models
                             # or openai | anthropic | duckduckgo | serpapi (needs a key) | "" (off)

[voice]
transcribe = "auto"          # Telegram voice notes: openai if its key is set, else local whisper if installed
                             # or openai | whisper | elevenlabs (needs a key) | "" (off)

[shell]
enabled = false              # true: the agent may run shell commands, each one only after you say yes
timeout_seconds = 30

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
                         lambda values: __import__('local_agent.daemon.heartbeat', fromlist=['_']).check(values)),
    # <<< capabilities
    # >>> reach
    'remote': Section({'allowed_hosts': []},
                      lambda values: __import__('local_agent.daemon.security', fromlist=['_']).check_remote(values)),
    # <<< reach
    # >>> evals
    # <<< evals
    # >>> sandbox
    # <<< sandbox
}

AGENTS_MD = """\
# Standing orders

Anything here is in every run. Write it like a note to an assistant: who you are, how you like answers,
what to never do. Local Agent treats it as your own words.
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
    transcribe: str = 'auto'  # a TRANSCRIBERS name; '' turns voice notes off
    shell_enabled: bool = False
    shell_timeout: int = 30
    mcp_servers: tuple = ()  # mcp.config.McpServer, from the [mcp.servers.*] blocks
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
        return cls(Path(os.environ.get(HOME_ENV) or Path.home() / '.local-agent').expanduser())

    config_path = property(lambda self: self.root / 'config.toml')
    db_path = property(lambda self: self.root / 'state.db')
    workspace = property(lambda self: self.root / 'workspace')
    agents_md = property(lambda self: self.workspace / 'AGENTS.md')
    memory_dir = property(lambda self: self.workspace / 'memory')
    skills_dir = property(lambda self: self.workspace / 'skills')
    files_dir = property(lambda self: self.workspace / 'files')

    def ensure(self) -> None:
        """Creates the folder layout and a default config on first use. Never overwrites anything."""
        for folder in (self.root, self.memory_dir, self.skills_dir, self.files_dir):
            folder.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)  # memory and the event log are private: only the owner's account can read them
        if not self.config_path.exists():
            self.config_path.write_text(DEFAULT_CONFIG)
        if not self.agents_md.exists():
            self.agents_md.write_text(AGENTS_MD)


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
        for key in table.keys() - section.defaults.keys():
            _warn_once(f'[{name}] {key} is not a setting; it is ignored.')
        values = {**section.defaults, **{key: value for key, value in table.items() if key in section.defaults}}
        for key, value in values.items():
            default = section.defaults[key]
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
    if not isinstance(shell.get('enabled', False), bool):
        raise ConfigError('[shell] enabled must be true or false.')
    if 'answer_strangers' in telegram:
        _warn_once('[telegram] answer_strangers is gone: the bot only ever answers its paired owner. '
                   f'You can delete that line from {home.config_path}.')
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
    return Config(
        sections=load_sections(data),
        mcp_servers=mcp_servers,
        daemon_port=port,
        dev_origin=dev_origin,
        model=ModelConfig(provider=provider, default=model.get('default', ModelConfig.default),
                          strong=model.get('strong', ModelConfig.strong), base_url=model.get('base_url', '')),
        run_tokens=_positive_or_none(budget.get('run_tokens', Config.run_tokens), 'run_tokens'),
        run_tool_calls=_positive_or_none(budget.get('run_tool_calls', Config.run_tool_calls), 'run_tool_calls'),
        daily_tokens=_positive_or_none(budget.get('daily_tokens'), 'daily_tokens'),
        owner_email=owner.get('email', ''),
        timezone=timezone,
        search=search,
        transcribe=transcribe,
        shell_enabled=shell.get('enabled', False),
        shell_timeout=_positive_or_none(shell.get('timeout_seconds', Config.shell_timeout), 'timeout_seconds')
        or Config.shell_timeout,
    )


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

def set_key(provider: str, value: str) -> None:
    keyring.set_password(APP, provider, value)


def get_key(provider: str) -> str | None:
    """The provider's key from the keychain, else its environment variable. Registered for redaction."""
    value = keyring.get_password(APP, provider) or os.environ.get(KEY_ENV.get(provider, ''), '') or None
    if value:
        secrets.register_secret(value)  # a key the process holds must never reach the model or the log
    return value
