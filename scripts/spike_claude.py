"""
Phase 0 spike (plans/2026-11-04-subscription-mode): one tool call from Claude Code into this Python process and
back, on your own Claude login. Needs `uv sync --extra claude` and `claude` signed in with /login. It prints the
credential Claude Code chose (`none` is the login), the echo result, and how long start and finish took.
Session saving is off, and none of your Claude Code settings are loaded.
"""

import asyncio
import time

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, SystemMessage, create_sdk_mcp_server, query, tool


@tool('echo', 'Echo text back', {'text': str})
async def echo(args):
    return {'content': [{'type': 'text', 'text': f'echo: {args["text"]}'}]}


async def main():
    options = ClaudeAgentOptions(
        tools=[],                                    # no built-in Claude Code tools
        mcp_servers={'wraithd': create_sdk_mcp_server('wraithd', tools=[echo])},
        allowed_tools=['mcp__wraithd__echo'],
        strict_mcp_config=True, setting_sources=[],  # ignore your other Claude Code settings
        env={'ANTHROPIC_API_KEY': ''},               # use the login, never a key
        extra_args={'no-session-persistence': None},  # leave no session files in ~/.claude
        model='haiku',                               # with no model, Claude Code picks Opus
        max_turns=4)
    started = time.monotonic()
    async for message in query(prompt='Call echo with "hi", then stop.', options=options):
        if isinstance(message, SystemMessage) and message.subtype == 'init':
            print('auth:', message.data.get('apiKeySource'), f'{time.monotonic() - started:.1f}s')
        elif isinstance(message, ResultMessage):
            print(message.result, message.num_turns, f'{time.monotonic() - started:.1f}s')

asyncio.run(main())
