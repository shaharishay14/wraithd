"""
Fakes for running the harness through the real Agents SDK Runner without a real model: a scripted model that
plays back canned turns, fake tools, and a small run() that wires the harness together the way the real one
will (Phase 2): gate, event log, reminders, loop.
"""

import json
from types import SimpleNamespace

from agents import Agent, FunctionTool, set_tracing_disabled
from agents.items import ModelResponse
from agents.models.interface import Model
from agents.usage import Usage
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
    ResponseTextDeltaEvent,
)

from local_agent.core import events as ev
from local_agent.core import reminders
from local_agent.core.gate import PermissionGate, guard_function_tools, parse_arguments
from local_agent.core.loop import run_with_step_limit
from local_agent.core.stop import StopPolicy

set_tracing_disabled(True)


class ScriptedModel(Model):
    """Stands in for the LLM inside the real Agents SDK Runner: plays back one list of output items per turn (or
    raises, if the turn is an exception), and records the input and tool names the Runner sent each time.
    Streams text in small pieces, like a real model."""

    def __init__(self, turns, usage=None):
        self.turns = list(turns)
        self.usage = usage  # Usage to report on every call, for budget tests
        self.inputs = []
        self.tool_names = []
        self.instructions = []

    async def get_response(self, system_instructions, input, model_settings, tools, output_schema, handoffs,
                           tracing, **kwargs):
        self.inputs.append(input)
        self.instructions.append(system_instructions)
        self.tool_names.append(sorted(tool.name for tool in tools))
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return ModelResponse(output=turn, usage=self.usage or Usage(), response_id=None)

    async def stream_response(self, system_instructions, input, model_settings, tools, output_schema, handoffs,
                              tracing, **kwargs):
        response = await self.get_response(system_instructions, input, model_settings, tools, output_schema,
                                           handoffs, tracing)
        sequence = 0
        for item in response.output:
            if isinstance(item, ResponseOutputMessage):
                text = item.content[0].text
                for start in range(0, len(text), 4):
                    yield ResponseTextDeltaEvent(content_index=0, delta=text[start:start + 4], item_id=item.id,
                                                 logprobs=[], output_index=0, sequence_number=sequence,
                                                 type='response.output_text.delta')
                    sequence += 1
        usage = response.usage
        yield ResponseCompletedEvent(
            response=Response(id='resp', created_at=0, model='scripted', object='response', output=response.output,
                              parallel_tool_calls=False, tool_choice='auto', tools=[],
                              usage={'input_tokens': usage.input_tokens, 'output_tokens': usage.output_tokens,
                                     'total_tokens': usage.total_tokens,
                                     'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0},
                                     'output_tokens_details': {'reasoning_tokens': 0}}),
            sequence_number=sequence, type='response.completed')

    def tool_outputs(self, turn):
        """Tool results the Runner sent to the model at the start of a turn, by call id."""
        return {item['call_id']: item['output'] for item in self.inputs[turn]
                if isinstance(item, dict) and item.get('type') == 'function_call_output'}


def call(call_id, name, arguments):
    """A tool call made by the scripted model."""
    return ResponseFunctionToolCall(id=f'fc_{call_id}', call_id=f'call_{call_id}', name=name,
                                    arguments=json.dumps(arguments), type='function_call', status='completed')


def say(text):
    """A final text answer from the scripted model."""
    return ResponseOutputMessage(id='msg_1', role='assistant', status='completed', type='message',
                                 content=[ResponseOutputText(text=text, type='output_text', annotations=[])])


def fake_tool(name, fn=None, schema=None, description='A test tool.'):
    """
    A tool like the harness's built-ins: fn(**arguments) returns the data; an exception becomes an {"error"}
    result the model can read (the built-ins never raise into the loop).
    """
    fn = fn or (lambda **arguments: {'ok': True})

    async def invoke(ctx, input_json):
        try:
            result = fn(**parse_arguments(input_json))
        except Exception as e:
            return json.dumps({'error': f'{type(e).__name__}: {e}'})
        return result if isinstance(result, str) else json.dumps(result)

    return FunctionTool(name=name, description=description,
                        params_json_schema=schema or {'type': 'object', 'properties': {}},
                        on_invoke_tool=invoke, strict_json_schema=False)


def tool_json(output):
    """A tool result as the model saw it, parsed, without any system reminder the harness added after it."""
    return json.loads(output.split('\n<system-reminder>')[0])


async def run_agent(model, tools, message, *, store, callback=None, gate=None, policy=None, history=(),
                    owner=True, skills=(), previous_run=(), source='chat', wrap=None):
    """
    One run, wired like the real run() will be: a gate, an event log, tool-bound reminders, the loop.
    wrap(guarded_tools, gate, log, after_result) may replace how tools are guarded (discovery does).
    Returns (answer, log); the log is saved to the store.
    """
    log = ev.EventLog(store)
    log.append(ev.RUN_START, source=source, message=message, untrusted_inputs=[])
    gate = gate or PermissionGate(store, callback, list(history), message)
    situation = reminders.Situation(message=message, events=log.events, owner=owner, skills=list(skills),
                                    previous_run=list(previous_run))

    def after_result(tool_name, result_text):
        situation.tool, situation.tool_output = tool_name, result_text
        return reminders.inject(log, tool_name, reminders.due(reminders.REMINDERS, tool_name, situation))

    guarded = (wrap or guard_function_tools)(tools, gate, log, after_result)
    log.append(ev.CONTEXT_BUILT, model='scripted', tools_offered=[tool.name for tool in guarded])
    chat = list(history) + [{'role': 'user', 'content': message}]
    start_block = reminders.inject(log, reminders.RUN_START, reminders.due(reminders.REMINDERS, reminders.RUN_START,
                                                                           situation))
    if start_block:
        chat.append({'role': 'user', 'content': start_block})
    agent = Agent(name='test', instructions='You help the owner.' + reminders.PREAMBLE, tools=guarded, model=model)
    try:
        result = await run_with_step_limit(agent, chat, callback, log=log, policy=policy or StopPolicy.default())
        answer = result.final_output
        log.append(ev.RUN_END, outcome='answered', turns=len(log.of_type(ev.TURN_START)),
                   stop_reason=next((e.payload['reason'] for e in reversed(log.events) if e.type == ev.STOP), None))
        return answer, log
    finally:
        log.save()


def recorder():
    """A recording callback plus the list it records into."""
    recorded = []

    async def callback(event_type, data):
        recorded.append((event_type, data))

    callback.recorded = recorded
    return callback


def skill(name, description):
    """A skill as the reminders see one: a name and a description."""
    return SimpleNamespace(name=name, description=description)
