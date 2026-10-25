"""
Connecting to the owner's MCP servers for a run (or a terminal session), with the Agents SDK's own MCP clients
(MCPServerStdio, MCPServerStreamableHttp). Each server's tools become SDK tools named SERVER_TOOL, so the
disclosure catalog groups them by server, and each tool gets its gate tier:

- annotated read-only by the server: runs freely
- anything else: a write, asked about each time, unless the owner marked it "always allow"

What a server says about itself (tool descriptions, its instructions) is third-party text: it's capped and the
harness's own tags are neutralized before it reaches the model, like any tool output.
"""

import asyncio
import json
import logging
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass, field

from agents import FunctionTool
from agents.mcp import MCPServerStdio, MCPServerStreamableHttp

from local_agent.core.gate import ToolAccess, parse_arguments
from local_agent.mcp.config import McpConfigError, McpServer, get_secrets

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 20
CALL_TIMEOUT_SECONDS = 60
DESCRIPTION_CHARS = 1000
INSTRUCTIONS_CHARS = 2000
MAX_NAME = 64  # what model APIs accept for a tool name


def neutralize(text: str, limit: int) -> str:
    """Third-party text about a tool: capped, and unable to pose as the harness's own markup."""
    text = re.sub(r'<(/?)(system-reminder|untrusted_tool_output)', r'<\1\2_', text or '', flags=re.IGNORECASE)
    return text if len(text) <= limit else text[:limit - 3] + '...'


def tool_name(server: str, tool: str) -> str:
    """SERVER_TOOL in the shape model APIs accept: GMAIL_SEND_EMAIL. The server part keeps its dashes."""
    clean = re.sub(r'[^A-Za-z0-9]+', '_', tool).strip('_').upper() or 'TOOL'
    return f'{server.upper()}_{clean}'[:MAX_NAME]


def result_text(result) -> str:
    """An MCP CallToolResult as the text the harness renders: the text parts, and an error marked as one."""
    parts = []
    for item in result.content or []:
        text = getattr(item, 'text', None)
        parts.append(text if text is not None else f'[{getattr(item, "type", "content")} omitted]')
    text = '\n'.join(parts)
    if not text and getattr(result, 'structured_content', None) is not None:
        text = json.dumps(result.structured_content)
    if getattr(result, 'is_error', False):
        return json.dumps({'error': text or 'The tool reported an error.'})
    return text


@dataclass
class Connected:
    """What the run gets from the owner's MCP servers."""

    tools: list[FunctionTool] = field(default_factory=list)
    access: dict[str, ToolAccess] = field(default_factory=dict)  # registered name -> tier
    always_allowed: set[str] = field(default_factory=set)  # registered names the owner allowed
    guides: dict[str, str] = field(default_factory=dict)  # SERVER -> its instructions
    provides: set[str] = field(default_factory=set)  # capabilities of the servers that connected
    errors: dict[str, str] = field(default_factory=dict)  # server -> why it didn't connect
    origin: dict[str, tuple[str, str]] = field(default_factory=dict)  # registered name -> (server, tool)


def sdk_server(server: McpServer):
    """The SDK client for one server, with only this server's secrets."""
    env, headers = get_secrets(server)
    if server.transport == 'stdio':
        # The SDK starts the process with a minimal environment (PATH, HOME...) plus this env, never the
        # owner's whole environment: other servers' tokens and the model keys stay out
        return MCPServerStdio({'command': server.command, 'args': list(server.args), 'env': env}, name=server.name,
                              client_session_timeout_seconds=CALL_TIMEOUT_SECONDS)
    return MCPServerStreamableHttp({'url': server.url, 'headers': headers}, name=server.name,
                                   client_session_timeout_seconds=CALL_TIMEOUT_SECONDS)


def _wrap(sdk, server: McpServer, tool, registered: str) -> FunctionTool:
    async def invoke(ctx, input_json: str) -> str:
        try:
            result = await sdk.call_tool(tool.name, parse_arguments(input_json))
        except Exception as e:
            logger.warning(f'{server.name}/{tool.name} failed: {type(e).__name__}')
            return json.dumps({'error': f'{server.name} could not run {tool.name} ({type(e).__name__}). Do not retry '
                                        'more than once; tell the user if it keeps failing.'})
        return result_text(result)

    return FunctionTool(name=registered, description=neutralize(tool.description or tool.title or tool.name,
                                                                 DESCRIPTION_CHARS),
                        params_json_schema=tool.input_schema or {'type': 'object', 'properties': {}},
                        on_invoke_tool=invoke, strict_json_schema=False)


class McpConnections:
    """
    async with McpConnections(servers) as connected: ... Connects every server, and closes them all on the way out.
    A server that fails to connect is left out (and reported in connected.errors), never fatal for the run.
    """

    def __init__(self, servers, factory=sdk_server):
        self.servers, self.factory = list(servers), factory
        self._stack = AsyncExitStack()

    async def __aenter__(self) -> Connected:
        connected = Connected()
        await self._stack.__aenter__()
        for server in self.servers:
            try:
                sdk = self.factory(server)
                await asyncio.wait_for(sdk.connect(), timeout=CONNECT_TIMEOUT_SECONDS)
                self._stack.push_async_callback(sdk.cleanup)
                listed = await asyncio.wait_for(sdk.list_tools(), timeout=CONNECT_TIMEOUT_SECONDS)
            except Exception as e:  # a broken server must not take the run down with it
                connected.errors[server.name] = str(e) if isinstance(e, McpConfigError) else type(e).__name__
                logger.warning(f'MCP server {server.name} did not connect: {connected.errors[server.name]}')
                continue
            instructions = getattr(getattr(sdk, 'session', None), 'instructions', None)
            if instructions:
                connected.guides[server.name.upper()] = neutralize(instructions, INSTRUCTIONS_CHARS)
            connected.provides |= set(server.provides)
            for tool in listed:
                registered = tool_name(server.name, tool.name)
                while registered in connected.access:  # two tool names that clean up the same
                    registered = registered[:MAX_NAME - 2] + '_2'
                read_only = bool(getattr(tool.annotations, 'read_only_hint', False))
                connected.access[registered] = ToolAccess.READ if read_only else ToolAccess.WRITE
                if tool.name in server.always_allow and not read_only:
                    connected.always_allowed.add(registered)
                connected.origin[registered] = (server.name, tool.name)
                connected.tools.append(_wrap(sdk, server, tool, registered))
        return connected

    async def __aexit__(self, *exc) -> None:
        try:
            await self._stack.__aexit__(*exc)
        except Exception as e:  # closing a server that already died shouldn't hide the run's own outcome
            logger.warning(f'Closing MCP servers: {type(e).__name__}')
