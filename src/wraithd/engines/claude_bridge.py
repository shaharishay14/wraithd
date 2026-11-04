"""
wraithd's guarded tools, handed to Claude Code as one in-process MCP server. Claude Code only ever calls tools that
went through guard_function_tools() (core/gate.py), so every call still goes through the gate: approval cards,
Telegram buttons, taint, redaction and the event log run exactly as on the Agents SDK engine.

One bridge per Claude Code session, built when it connects (its tool names are fixed from then on); a RunSlot says
which run's guarded tools a call goes to right now. Between runs the slot is empty and every call is refused, so a
warm session can never act with an earlier run's gate.

The Agents SDK's tool-call limit and stop rules live in its run hooks, which this engine doesn't use: the slot counts
calls itself, and asks the run's stop() before each one.
"""

import json
import logging
import uuid

from agents.tool_context import ToolContext

from wraithd.engines.claude_options import SERVER, sdk

logger = logging.getLogger(__name__)

NO_RUN_TEXT = 'No active run; tool refused.'
LIMIT_TEXT = 'Tool-call limit reached. Answer with what you have.'


class RunSlot:
    """Which run's guarded tools the Claude Code session may call right now, with that run's limits."""

    def __init__(self):
        self.tools, self.limit, self.stop, self.calls = {}, None, None, 0

    def attach(self, guarded, limit: int | None = None, stop=None) -> None:
        """guarded: the run's tools after guard_function_tools(). limit: calls per run. stop() -> str | None: a
        reason to refuse every further call (the run's stop rules), told to Claude Code so it answers now."""
        self.tools = {tool.name: tool for tool in guarded}
        self.limit, self.stop, self.calls = limit, stop, 0

    def detach(self) -> None:
        self.tools, self.stop = {}, None


def tool_names(guarded) -> list[str]:
    """The names Claude Code knows the bridged tools by."""
    return [f'mcp__{SERVER}__{tool.name}' for tool in guarded]


def _text(text: str, error: bool = False) -> dict:
    return {'content': [{'type': 'text', 'text': text}], **({'is_error': True} if error else {})}


def bridge(first_guarded, slot: RunSlot):
    """(MCP server, allowed tool names), built once per session from its first run's tools. Each call goes through
    the gate of the run in the slot."""
    claude = sdk()

    def wrap(name, description, schema):
        @claude.tool(name, description, schema)
        async def call(args):
            current = slot.tools.get(name)
            if current is None:
                return _text(NO_RUN_TEXT, error=True)
            reason = slot.stop() if slot.stop else None  # the run's stop rules, which log why they stopped it
            if reason:
                return _text(reason, error=True)
            slot.calls += 1
            if slot.limit and slot.calls > slot.limit:
                return _text(LIMIT_TEXT, error=True)
            input_json = json.dumps(args)
            ctx = ToolContext(context=None, tool_name=name, tool_call_id=uuid.uuid4().hex, tool_arguments=input_json)
            try:
                text = await current.on_invoke_tool(ctx, input_json)
            except Exception as e:  # the guard logged it; Claude Code gets the error, not a broken server
                logger.warning(f'Tool {name} failed: {type(e).__name__}: {e}')
                return _text(f'{type(e).__name__}: {e}', error=True)
            return _text(text if isinstance(text, str) else json.dumps(text, default=str))
        return call

    tools = [wrap(tool.name, tool.description or '', tool.params_json_schema) for tool in first_guarded]
    return claude.create_sdk_mcp_server(SERVER, tools=tools), tool_names(first_guarded)
