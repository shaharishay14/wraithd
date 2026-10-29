"""
Fakes for the efficiency tests: a scripted model that reports token usage the way a provider does (it counts the
request it was sent, with its own framing), and cache reads and writes.
"""

import json

import tiktoken
from agents.usage import InputTokensDetails, Usage
from openai.types.responses import ResponseCompletedEvent

from local_agent.core import tokens
from tests.scripted import ScriptedModel

PER_MESSAGE = 4  # role and separators, as chat formats frame each message
PER_TOOL = 8


def provider_count(system, input_items, tools) -> int:
    """
    What a provider would report as input tokens: every message's text plus its framing, and the tool schemas as
    JSON. Counted here independently of core/tokens.py (its own serialization), so the meter is checked against
    something it didn't compute.
    """
    tokens._get_encoder()  # sets TIKTOKEN_CACHE_DIR to LiteLLM's bundled files: no download
    encoder = tiktoken.get_encoding('o200k_base')

    def n(text):
        return len(encoder.encode(text, disallowed_special=()))

    total = n(system or '') + PER_MESSAGE
    items = [{'role': 'user', 'content': input_items}] if isinstance(input_items, str) else input_items
    for item in items:
        item = item.model_dump() if hasattr(item, 'model_dump') else item
        if item.get('type') == 'function_call_output':
            total += n(str(item.get('output', ''))) + PER_MESSAGE
        elif item.get('type') == 'function_call':
            total += n(item.get('name', '')) + n(item.get('arguments', '')) + PER_MESSAGE
        else:
            content = item.get('content')
            if isinstance(content, list):
                content = ''.join(part.get('text', '') for part in content if isinstance(part, dict))
            total += n(str(content or '')) + PER_MESSAGE
    for tool in tools or []:
        total += n(json.dumps({'name': tool.name, 'description': tool.description,
                               'parameters': tool.params_json_schema})) + PER_TOOL
    return total


class CountingModel(ScriptedModel):
    """A ScriptedModel whose usage is what a provider would report for each request, with cache reads and writes."""

    def __init__(self, turns, cache_read=0, cache_write=0, output_tokens=5, name='scripted'):
        super().__init__(turns, name=name)
        self.cache_read, self.cache_write, self.output_tokens = cache_read, cache_write, output_tokens
        self.reported = []

    async def get_response(self, system_instructions, input, model_settings, tools, output_schema, handoffs,
                           tracing, **kwargs):
        count = provider_count(system_instructions, input, tools)
        self.reported.append(count)
        self.usage = Usage(requests=1, input_tokens=count, output_tokens=self.output_tokens,
                           total_tokens=count + self.output_tokens,
                           input_tokens_details=InputTokensDetails(cached_tokens=self.cache_read,
                                                                   cache_write_tokens=self.cache_write))
        return await super().get_response(system_instructions, input, model_settings, tools, output_schema,
                                          handoffs, tracing)

    async def stream_response(self, *args, **kwargs):
        async for event in super().stream_response(*args, **kwargs):
            if isinstance(event, ResponseCompletedEvent):
                details = event.response.usage.input_tokens_details
                event.response.usage.input_tokens_details = details.model_copy(
                    update={'cached_tokens': self.cache_read, 'cache_write_tokens': self.cache_write})
            yield event
