"""
What offering the recently used app actions directly costs per model call: their schemas in the tool list, and one
changed sentence in the discovery note. Paid on every turn (cached after the first), saved: the find and describe
turns before each action. The budget is what a typical calendar set measures; a change that grows it fails.
"""

import json

from agents import FunctionTool

from tests.scripted import ScriptedModel, run_once, say
from wraithd.core import disclosure, tokens
from wraithd.core import events as ev

# Shaped like the owner's calendar and Gmail servers' read actions (argument names and descriptions)
GET_EVENTS = {'type': 'object', 'properties': {
    'calendar_id': {'type': 'string', 'description': "Calendar id, or 'primary' for the user's main calendar."},
    'time_min': {'type': 'string', 'description': 'Start of the range, RFC3339 with offset, e.g. '
                                                  '2026-11-10T00:00:00+02:00.'},
    'time_max': {'type': 'string', 'description': 'End of the range (exclusive), RFC3339 with offset.'},
    'query': {'type': 'string', 'description': 'Free text to match in the summary, description or location.'},
    'max_results': {'type': 'integer', 'description': 'At most this many events.', 'default': 50},
    'single_events': {'type': 'boolean', 'description': 'Expand recurring events into instances.',
                      'default': True},
    'order_by': {'type': 'string', 'enum': ['startTime', 'updated'], 'default': 'startTime'},
    'timezone': {'type': 'string', 'description': 'IANA timezone for the response, e.g. Asia/Jerusalem.'},
}, 'required': ['time_min', 'time_max']}
SEARCH_EMAILS = {'type': 'object', 'properties': {
    'query': {'type': 'string', 'description': 'Gmail search, e.g. "is:unread from:dana newer_than:2d".'},
    'max_results': {'type': 'integer', 'description': 'At most this many messages.', 'default': 10},
    'label_ids': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Only messages with these labels.'},
    'page_token': {'type': 'string', 'description': 'From the previous page, to get the next one.'},
    'include_body': {'type': 'boolean', 'description': 'Include each message body, not only the snippet.',
                     'default': False},
}}
RECENT = {
    'GOOGLE_GET_EVENTS': ('List the events in one calendar between two times. Returns each event\'s summary, start, '
                          'end, location, attendees and link.', GET_EVENTS),
    'GMAIL_SEARCH_EMAILS': ('Search the mailbox with Gmail search syntax. Returns the matching messages\' sender, '
                            'subject, date and snippet.', SEARCH_EMAILS),
}
# Tokens (core/tokens.py, o200k) the two actions add to each model call: 501 measured (GOOGLE_GET_EVENTS 299,
# GMAIL_SEARCH_EMAILS 195, the note's changed sentence 7), 1,748 tool schema chars. Servers with longer descriptions
# cost more: context_built's tool_schema_chars shows a real run's.
BUDGET = 501 + 20


def app_tools() -> list[FunctionTool]:
    async def invoke(ctx, payload):
        return json.dumps({'ok': True})
    actions = {**RECENT, **{f'SLACK_ACTION_{i}': (f'Slack action {i}.', {'type': 'object', 'properties': {}})
                            for i in range(disclosure.DIRECT_ACTIONS_LIMIT)}}
    return [FunctionTool(name=name, description=description, params_json_schema=schema, on_invoke_tool=invoke,
                         strict_json_schema=False) for name, (description, schema) in actions.items()]


def remember(store) -> None:
    from datetime import datetime, timedelta
    when = datetime.now().astimezone() - timedelta(days=1)
    store.save_events('yesterday', [ev.Event(0, ev.RUN_START, {'source': 'chat', 'message': 'x', 'owner': True}, when),
                                    *(ev.Event(i + 1, ev.TOOL_CALL, {'tool': name, 'allowed': True}, when)
                                      for i, name in enumerate(RECENT))])


async def first_call(make_runtime, recent: bool):
    tools_seen = []

    class Model(ScriptedModel):
        async def get_response(self, system_instructions, input, model_settings, tools, *args, **kwargs):
            tools_seen.append(list(tools))
            return await super().get_response(system_instructions, input, model_settings, tools, *args, **kwargs)

    model = Model([[say('ok')]])
    rt = make_runtime(model)
    if recent:
        remember(rt.store)
    _, events = await run_once(rt, 'what is on my calendar tomorrow?', builtins=False, app_tools=app_tools())
    tool_tokens = sum(tokens.count(tokens.tool_schema(tool)) for tool in tools_seen[0])
    return tool_tokens + tokens.count(model.instructions[0]), of_type_built(events)


def of_type_built(events):
    return next(event.payload for event in events if event.type == ev.CONTEXT_BUILT)


async def test_recent_calendar_and_mail_actions_cost_within_budget(make_runtime):
    without, built_without = await first_call(make_runtime, recent=False)
    with_recent, built = await first_call(make_runtime, recent=True)
    assert sorted(built['recent_actions']) == sorted(RECENT) and 'recent_actions' not in built_without
    added = with_recent - without
    print(f'recent actions add {added} tokens per call, '
          f'{built["tool_schema_chars"] - built_without["tool_schema_chars"]} tool schema chars')
    assert 0 < added <= BUDGET, f'the recent actions add {added} tokens per call, over their budget of {BUDGET}'
