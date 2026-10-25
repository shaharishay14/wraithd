"""
Chat in the terminal: the owner types, the answer streams back, and write actions ask y/N first.

One session is one conversation. Its messages are stored, so long sessions are compacted, and when the session
ends, distill turns it into memory: that's how "I prefer short replies" holds the next time.
"""

import asyncio
import json
import sys

from local_agent.core import compaction
from local_agent.mcp.client import McpConnections
from local_agent.memory import distill
from local_agent.run import Runtime, run
from local_agent.store import base as store_base

QUIT = {'/quit', '/exit', 'quit', 'exit', ':q'}
DISTILL_MIN_MESSAGES = 2  # at the end of a session, one exchange is enough to learn from


class Terminal:
    """Reading and writing, so tests can play the owner."""

    async def ask(self, prompt: str) -> str | None:
        """A line from the owner, or None when input ends (Ctrl-D)."""
        try:
            return await asyncio.to_thread(input, prompt)
        except EOFError:
            return None

    def say(self, text: str = '', end: str = '\n') -> None:
        sys.stdout.write(text + end)
        sys.stdout.flush()


def _approval_card(data: dict) -> str:
    lines = [f'\nThe agent wants to run {data["tool"]}:']
    lines += [f'  {key}: {json.dumps(value) if not isinstance(value, str) else value}'
              for key, value in data['arguments'].items()]
    if data.get('reason'):
        lines.append(f'  {data["reason"]}')
    for recipient in data.get('suspicious_recipients') or []:
        lines.append(f'  ! {recipient["value"]} was not written by you (it came from {recipient["source"]})')
    if data.get('untrusted_sources') and not data.get('suspicious_recipients'):
        lines.append(f'  ! This run read third-party content first: {", ".join(data["untrusted_sources"])}')
    return '\n'.join(lines)


def make_callback(rt: Runtime, terminal: Terminal, state: dict):
    """Streams text as it's written, shows tool calls, and turns approval cards into a y/N question."""

    async def callback(event_type: str, data: dict) -> None:
        if event_type == 'content_delta':
            state['streamed'] = True
            terminal.say(data['content'], end='')
        elif event_type == 'tool_call_start':
            terminal.say(f'  · {data["tool"]}')
        elif event_type == 'approval_required':
            terminal.say(_approval_card(data))
            answer = (await terminal.ask('Allow? [y/N] ') or '').strip().lower()
            decision = store_base.APPROVED if answer in ('y', 'yes') else store_base.DENIED
            rt.store.decide_approval(data['approval_id'], decision)

    return callback


async def chat(rt: Runtime, terminal: Terminal | None = None) -> str:
    """One session. Returns the conversation id."""
    terminal = terminal or Terminal()
    async with McpConnections(rt.config.mcp_servers) as mcp:  # once per session, not once per message
        for name, problem in mcp.errors.items():
            terminal.say(f'(MCP server {name} is not available this session: {problem})')
        return await _session(rt, terminal, mcp)


async def _session(rt: Runtime, terminal: Terminal, mcp) -> str:
    conversation = rt.store.start_conversation('terminal')
    terminal.say('Local Agent. Type /quit to end; what you share is remembered for next time.')
    try:
        while True:
            message = await terminal.ask('\n> ')
            if message is None or message.strip().lower() in QUIT:
                break
            if not message.strip():
                continue
            rt.store.add_message(conversation.id, 'user', message)
            history = await compaction.history_for(rt.store, conversation.id, rt.complete)
            state = {'streamed': False}
            result = await run(rt, message, history=history, callback=make_callback(rt, terminal, state),
                               source='chat', mcp=mcp)
            # A streamed answer is already on screen; a refusal or failure message never streamed
            terminal.say('' if state['streamed'] else result.answer)
            rt.store.add_message(conversation.id, 'assistant', result.answer, run_id=result.run_id)
    finally:
        try:
            outcome = await distill.distill_conversation(rt.store, rt.memory, rt.complete, conversation.id,
                                                         min_messages=DISTILL_MIN_MESSAGES)
            if outcome.get('notes'):
                terminal.say(f'Remembered: {", ".join(outcome["notes"])}')
        except Exception as e:
            terminal.say(f'(Could not save what I learned this time: {type(e).__name__})')
    return conversation.id
