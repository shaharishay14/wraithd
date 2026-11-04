"""
A stand-in for claude_agent_sdk, offline: the same names and fields wraithd uses, and a query() that plays a script.
No Claude Code process starts, no model is called, nothing in ~/.claude is touched.

A script is a list of steps for one query() call. Each step is a message to yield, or an async function
step(options) that may call the bridge's tools (options.mcp_servers) and returns a message (or None).
"""

import types
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ClaudeAgentOptions:
    tools: Any = None
    allowed_tools: list = field(default_factory=list)
    system_prompt: Any = None
    mcp_servers: Any = field(default_factory=dict)
    strict_mcp_config: bool = False
    max_turns: int | None = None
    model: str | None = None
    env: dict = field(default_factory=dict)
    extra_args: dict = field(default_factory=dict)
    include_partial_messages: bool = False
    setting_sources: list | None = None
    permission_mode: str | None = None


@dataclass
class TextBlock:
    text: str


@dataclass
class SystemMessage:
    subtype: str
    data: dict


@dataclass
class AssistantMessage:
    content: list
    model: str = 'claude-sonnet'
    error: str | None = None
    usage: dict | None = None
    message_id: str | None = None


@dataclass
class StreamEvent:
    uuid: str
    session_id: str
    event: dict
    parent_tool_use_id: str | None = None


@dataclass
class RateLimitInfo:
    status: str
    resets_at: int | None = None


@dataclass
class RateLimitEvent:
    rate_limit_info: RateLimitInfo
    uuid: str = 'u'
    session_id: str = 's'


@dataclass
class ResultMessage:
    subtype: str = 'success'
    duration_ms: int = 10
    duration_api_ms: int = 5
    is_error: bool = False
    num_turns: int = 1
    session_id: str = 'session-1'
    usage: dict | None = None
    result: str | None = None
    errors: list | None = None
    api_error_status: int | None = None


@dataclass
class SdkMcpTool:
    name: str
    description: str
    input_schema: Any
    handler: Any


class CLINotFoundError(Exception):
    pass


def tool(name, description, input_schema, annotations=None):
    def decorate(handler):
        return SdkMcpTool(name, description, input_schema, handler)
    return decorate


def create_sdk_mcp_server(name, version='1.0.0', tools=None):
    return {'type': 'sdk', 'name': name, 'tools': {t.name: t for t in tools or []}}


def init(source='none'):
    return SystemMessage('init', {'apiKeySource': source, 'session_id': 'session-1'})


def text_delta(text):
    return StreamEvent('u', 'session-1', {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': text}})


def result(text='done', usage=None, **extra):
    return ResultMessage(result=text, usage=usage or {'input_tokens': 100, 'output_tokens': 20,
                                                      'cache_read_input_tokens': 30,
                                                      'cache_creation_input_tokens': 5}, **extra)


def call(name, arguments, server='wraithd'):
    """A step: Claude Code calls one of the bridge's tools; yields an AssistantMessage naming what came back."""
    async def step(options):
        found = options.mcp_servers[server]['tools'][name]
        answer = await found.handler(arguments)
        step.answers.append(answer)
        return AssistantMessage([TextBlock(f'called {name}')], message_id=f'm-{name}')
    step.answers = []
    return step


class FakeClient:
    """ClaudeSDKClient, offline: each query() plays the next script; connects and disconnects are counted."""

    instances: list = []  # replaced per module()

    def __init__(self, options):
        self.options, self.connects, self.disconnects, self.texts = options, 0, 0, []
        self.script = None
        type(self).instances.append(self)

    async def connect(self):
        self.connects += 1

    async def disconnect(self):
        self.disconnects += 1

    async def query(self, text):
        self.texts.append(text)
        self.script = self.next_script()

    async def receive_response(self):
        for step in self.script:
            message = await step(self.options) if callable(step) else step
            if isinstance(message, BaseException):
                raise message
            if message is not None:
                yield message


def module(*scripts):
    """A fake claude_agent_sdk whose query() plays one script per call, in order. module.calls has each options."""
    fake = types.ModuleType('claude_agent_sdk')
    for name, value in globals().items():
        if (isinstance(value, type) and value is not FakeClient) or name in ('tool', 'create_sdk_mcp_server'):
            setattr(fake, name, value)
    fake.calls = []
    remaining = list(scripts)

    async def query(*, prompt, options):
        fake.calls.append({'prompt': prompt, 'options': options})
        script = remaining.pop(0) if remaining else [init(), result()]
        for step in script:
            message = await step(options) if callable(step) else step
            if isinstance(message, BaseException):
                raise message
            if message is not None:
                yield message

    fake.query = query

    class ClaudeSDKClient(FakeClient):
        instances = []

        def next_script(self):
            fake.calls.append({'prompt': self.texts[-1], 'options': self.options})
            return remaining.pop(0) if remaining else [init(), result()]

    fake.ClaudeSDKClient = ClaudeSDKClient
    return fake
