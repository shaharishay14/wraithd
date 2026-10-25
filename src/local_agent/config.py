"""
Where Local Agent keeps its state, and the owner's settings.

Everything lives in one folder, ~/.local-agent/ (or $LOCAL_AGENT_HOME):

    config.toml     model, budgets, owner, web search, shell (no secrets: those are in the OS keychain)
    state.db        the event log, conversations, approvals
    workspace/      AGENTS.md (standing orders), memory/, skills/, files/

API keys go in the keychain through `keyring`, never in the config file, so the folder can be backed up or
shared without leaking them.
"""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import keyring

from local_agent.core import secrets
from local_agent.mcp.config import McpConfigError, parse_servers

APP = 'local-agent'  # the keychain service name
HOME_ENV = 'LOCAL_AGENT_HOME'

PROVIDERS = ('openai', 'anthropic', 'ollama', 'openai-compatible')
SEARCH_PROVIDERS = ('serpapi',)
# Where a key comes from when it isn't in the keychain (and the variable is set)
KEY_ENV = {'openai': 'OPENAI_API_KEY', 'anthropic': 'ANTHROPIC_API_KEY', 'serpapi': 'SERPAPI_KEY',
           'telegram': 'TELEGRAM_BOT_TOKEN'}
# what `local-agent key set` takes
KEY_NAMES = tuple(p for p in PROVIDERS if p != 'ollama') + SEARCH_PROVIDERS + ('telegram',)

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
search = "serpapi"           # the web_search tool appears once a key is set: `local-agent key set serpapi`

[shell]
enabled = false              # true: the agent may run shell commands, each one only after you say yes
timeout_seconds = 30

[daemon]
port = 7878                  # the API and web UI, on 127.0.0.1 only
# dev_origin = "http://localhost:5173"   # a frontend dev server, while developing the UI
"""

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
    search: str = 'serpapi'  # '' turns web search off
    shell_enabled: bool = False
    shell_timeout: int = 30
    mcp_servers: tuple = ()  # mcp.config.McpServer, from the [mcp.servers.*] blocks
    daemon_port: int = 7878
    dev_origin: str = ''


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


def _positive_or_none(value, name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f'{name} must be a positive whole number (or left out for the default).')
    return value


def load_config(home: Home) -> Config:
    """The owner's config, with defaults for anything not set."""
    try:
        data = tomllib.loads(home.config_path.read_text()) if home.config_path.exists() else {}
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f'{home.config_path} is not valid TOML: {e}') from None
    model, budget, owner = data.get('model', {}), data.get('budget', {}), data.get('owner', {})
    web, shell, daemon = data.get('web', {}), data.get('shell', {}), data.get('daemon', {})
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
    if search and search not in SEARCH_PROVIDERS:
        raise ConfigError(f'[web] search must be one of {", ".join(SEARCH_PROVIDERS)}, or "" for none.')
    if not isinstance(shell.get('enabled', False), bool):
        raise ConfigError('[shell] enabled must be true or false.')
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
        shell_enabled=shell.get('enabled', False),
        shell_timeout=_positive_or_none(shell.get('timeout_seconds', Config.shell_timeout), 'timeout_seconds')
        or Config.shell_timeout,
    )


# --- keys --------------------------------------------------------------------------------------------------

def set_key(provider: str, value: str) -> None:
    keyring.set_password(APP, provider, value)


def get_key(provider: str) -> str | None:
    """The provider's key from the keychain, else its environment variable. Registered for redaction."""
    value = keyring.get_password(APP, provider) or os.environ.get(KEY_ENV.get(provider, ''), '') or None
    if value:
        secrets.register_secret(value)  # a key the process holds must never reach the model or the log
    return value
