"""
Progressive disclosure of app actions: with many actions, the model gets find -> describe -> run instead of every
schema, the order is enforced in code, and every real action still goes through the permission gate and the
event log.
"""

import json

from agents import FunctionTool

from tests.scripted import ScriptedModel, call, of_type, run_once, say
from wraithd.core import disclosure
from wraithd.core import events as ev

SEND_SCHEMA = {'type': 'object', 'properties': {
    'recipient_email': {'type': 'string', 'description': 'Who gets the email'},
    'subject': {'type': 'string'},
    'body': {'type': 'string'},
    'is_html': {'type': 'boolean', 'default': False},
}, 'required': ['recipient_email', 'body']}
FETCH_SCHEMA = {'type': 'object', 'properties': {'query': {'type': 'string', 'description': 'Gmail search'}}}

# 15 actions: more than DIRECT_ACTIONS_LIMIT, so the run uses discovery
ACTIONS = {
    'GMAIL_SEND_EMAIL': ('Send an email to one or more recipients. Supports HTML.', SEND_SCHEMA),
    'GMAIL_FETCH_EMAILS': ('Fetch emails from the inbox, with Gmail search filters.', FETCH_SCHEMA),
    **{f'SLACK_LIST_CHANNELS_{i}': (f'List Slack channels, page {i}.', {'type': 'object', 'properties': {}})
       for i in range(13)},
}
GUIDES = {'gmail': 'Use Gmail search syntax: is:unread, from:dana.'}


def fake_app_tools(calls):
    def make(slug, description, schema):
        async def invoke(ctx, payload):
            calls.append((slug, json.loads(payload)))
            return json.dumps({'successful': True, 'data': {'slug': slug}})
        return FunctionTool(name=slug, description=description, params_json_schema=schema,
                            on_invoke_tool=invoke, strict_json_schema=False)
    return [make(slug, description, schema) for slug, (description, schema) in ACTIONS.items()]


async def discovery_run(make_runtime, turns, message, app_calls, callback=None, actions=None):
    """A run with many app actions: run() keeps them out of the tool list past DIRECT_ACTIONS_LIMIT."""
    model = ScriptedModel(turns)
    tools = fake_app_tools(app_calls)
    _, events = await run_once(make_runtime(model), message, callback=callback, owner=False, builtins=False,
                               app_tools=tools[:actions] if actions else tools, app_guides=GUIDES)
    return model, events


# --- the catalog -------------------------------------------------------------------------------------------

def test_find_ranks_name_matches_first_and_filters_by_app():
    catalog = disclosure.AppCatalog(fake_app_tools([]))
    assert catalog.find('send emails')[0] == 'GMAIL_SEND_EMAIL'  # plural still matches
    assert catalog.find('inbox') == ['GMAIL_FETCH_EMAILS']  # description words count too
    assert all(name.startswith('SLACK_') for name in catalog.find('', app='slack'))
    assert catalog.find('weather forecast') == []


def test_typos_get_suggestions():
    catalog = disclosure.AppCatalog(fake_app_tools([]))
    assert catalog.suggestions('GMAIL_SEND_EMAILS')[0] == 'GMAIL_SEND_EMAIL'


def test_parameters_are_compact_with_required_first():
    parameters = disclosure._parameters(SEND_SCHEMA)
    assert [p['name'] for p in parameters] == ['recipient_email', 'body', 'subject', 'is_html']
    assert parameters[0] == {'name': 'recipient_email', 'type': 'string', 'required': True,
                             'description': 'Who gets the email'}
    assert parameters[-1]['default'] is False


def test_discovery_costs_less_context_than_the_schemas_it_replaces():
    app_tools = fake_app_tools([])
    discovery = disclosure.discovery_tools(disclosure.AppCatalog(app_tools), ev.EventLog())
    assert disclosure.schema_chars(discovery) < disclosure.schema_chars(app_tools)
    assert len(ACTIONS) > disclosure.DIRECT_ACTIONS_LIMIT


def test_the_prompt_note_names_the_apps_and_the_order():
    note = disclosure.discovery_instructions(['GMAIL', 'SLACK'], 15)
    assert 'GMAIL, SLACK (15 actions)' in note and 'find_app_actions (search) -> describe_app_action' in note


# --- full runs ---------------------------------------------------------------------------------------------

async def test_many_actions_are_discovered_not_listed(make_runtime):
    app_calls = []
    model, events = await discovery_run(make_runtime, [
        [call('1', 'find_app_actions', {'query': 'fetch inbox emails'})],
        [call('2', 'describe_app_action', {'action': 'GMAIL_FETCH_EMAILS'})],
        [call('3', 'run_app_action', {'action': 'GMAIL_FETCH_EMAILS', 'arguments': {'query': 'is:unread'}})],
        [call('4', 'describe_app_action', {'action': 'GMAIL_SEND_EMAIL'})],
        [say('You have mail.')],
    ], 'any unread mail?', app_calls)

    # the model never saw an app schema: only the three discovery tools
    assert model.tool_names[0] == ['describe_app_action', 'find_app_actions', 'run_app_action']
    found = json.loads(model.tool_outputs(1)['call_1'])
    assert found['actions'][0] == {'action': 'GMAIL_FETCH_EMAILS', 'app': 'GMAIL',
                                   'summary': 'Fetch emails from the inbox, with Gmail search filters.',
                                   'access': 'read'}
    assert 'score' not in json.dumps(found) and 'describe_app_action' in found['hint']
    # the app's guide comes with its first describe only
    first, second = json.loads(model.tool_outputs(2)['call_2']), json.loads(model.tool_outputs(4)['call_4'])
    assert first['app_guide'] == GUIDES['gmail'] and 'app_guide' not in second
    assert second['access'] == 'needs approval'
    # the real action ran once, behind the gate, and its output is still marked as third-party
    assert app_calls == [('GMAIL_FETCH_EMAILS', {'query': 'is:unread'})]
    assert model.tool_outputs(3)['call_3'].startswith('<untrusted_tool_output tool="GMAIL_FETCH_EMAILS">')
    # the log has the real action, not the dispatcher
    assert [e.payload['tool'] for e in of_type(events, ev.TOOL_CALL)] == [
        'find_app_actions', 'describe_app_action', 'GMAIL_FETCH_EMAILS', 'describe_app_action']


async def test_an_action_must_be_described_before_it_runs(make_runtime):
    app_calls = []
    model, events = await discovery_run(make_runtime, [
        [call('1', 'run_app_action', {'action': 'GMAIL_FETCH_EMAILS', 'arguments': {}})],
        [say('I need to look it up first.')],
    ], 'fetch my mail', app_calls)

    assert app_calls == []
    assert 'has not been described' in json.loads(model.tool_outputs(1)['call_1'])['error']
    result = of_type(events, ev.TOOL_RESULT)[0].payload
    assert result['tool'] == 'GMAIL_FETCH_EMAILS' and result['outcome'] == 'error'  # counts toward stop conditions


async def test_missing_arguments_are_caught_before_the_app_is_called(make_runtime):
    app_calls = []
    model, _ = await discovery_run(make_runtime, [
        [call('1', 'describe_app_action', {'action': 'GMAIL_SEND_EMAIL'})],
        [call('2', 'run_app_action', {'action': 'GMAIL_SEND_EMAIL', 'arguments': {'subject': 'Hi', 'cc': 'x'}})],
        [say('Who should I send it to?')],
    ], 'send an email', app_calls)

    error = json.loads(model.tool_outputs(2)['call_2'])['error']
    assert 'missing required arguments: recipient_email, body' in error and 'unknown arguments: cc' in error
    assert app_calls == []


async def test_an_unknown_action_gets_suggestions(make_runtime):
    model, _ = await discovery_run(make_runtime, [
        [call('1', 'describe_app_action', {'action': 'gmail_send_emails'})],
        [say('Found it.')],
    ], 'send an email', [])
    described = json.loads(model.tool_outputs(1)['call_1'])
    assert described['did_you_mean'][0] == 'GMAIL_SEND_EMAIL'


async def test_a_write_through_discovery_still_needs_approval(make_runtime):
    app_calls = []
    model, events = await discovery_run(make_runtime, [
        [call('1', 'describe_app_action', {'action': 'GMAIL_SEND_EMAIL'})],
        [call('2', 'run_app_action', {'action': 'GMAIL_SEND_EMAIL',
                                      'arguments': {'recipient_email': 'dana@co.com', 'body': 'Hi'}})],
        [say('This needs your approval.')],
    ], 'email dana', app_calls)  # no callback: nobody is there to approve

    assert app_calls == []
    assert 'needs the user\'s approval' in json.loads(model.tool_outputs(2)['call_2'])['error']
    send = [e.payload for e in of_type(events, ev.TOOL_CALL) if e.payload['tool'] == 'GMAIL_SEND_EMAIL'][0]
    assert send['access'] == 'write' and send['allowed'] is False


async def test_the_run_records_the_mode_and_explains_it(make_runtime):
    model, events = await discovery_run(make_runtime, [[say('Hi')]], 'hi', [])
    context = of_type(events, ev.CONTEXT_BUILT)[0].payload
    assert context['disclosure'] == 'discovery' and context['app_actions'] == len(ACTIONS)
    assert 'CONNECTED APPS: GMAIL, SLACK (15 actions)' in model.instructions[0]


async def test_few_actions_are_still_given_directly(make_runtime):
    model, events = await discovery_run(make_runtime, [[say('Hi')]], 'hi', [], actions=2)
    assert 'GMAIL_SEND_EMAIL' in model.tool_names[0] and 'find_app_actions' not in model.tool_names[0]
    assert of_type(events, ev.CONTEXT_BUILT)[0].payload['disclosure'] == 'direct'


# --- recent actions: what the owner's runs used last week is offered directly ---------------------------------

def past_run(store, calls, days_ago=1, owner=True):
    """A finished run `days_ago` days back that called each (tool, allowed) in `calls`."""
    from datetime import datetime, timedelta
    when = datetime.now().astimezone() - timedelta(days=days_ago)
    run_id = f'past-{days_ago}-{owner}-{len(calls)}-{calls[0][0] if calls else ""}'
    events = [ev.Event(0, ev.RUN_START, {'source': 'chat', 'message': 'x', 'owner': owner}, when)]
    events += [ev.Event(i + 1, ev.TOOL_CALL, {'tool': tool, 'arguments': {}, 'allowed': allowed}, when)
               for i, (tool, allowed) in enumerate(calls)]
    store.save_events(run_id, events)


async def recent_run(rt, model, owner=True, mcp=None, **kwargs):
    if mcp is not None:
        return await run_once(rt, 'what is new?', owner=owner, builtins=False, mcp=mcp, **kwargs)
    return await run_once(rt, 'what is new?', owner=owner, builtins=False, app_tools=fake_app_tools([]),
                          app_guides=GUIDES, **kwargs)


async def test_an_action_used_yesterday_is_offered_directly(make_runtime):
    model = ScriptedModel([[call('1', 'GMAIL_FETCH_EMAILS', {'query': 'is:unread'})], [say('Two new emails.')]])
    rt = make_runtime(model)
    past_run(rt.store, [('GMAIL_FETCH_EMAILS', True), ('GMAIL_FETCH_EMAILS', True), ('get_weather', True)])
    _, events = await recent_run(rt, model)
    offered = model.tool_names[0]
    assert 'GMAIL_FETCH_EMAILS' in offered and 'get_weather' not in offered  # only app actions of this run
    assert {'find_app_actions', 'describe_app_action', 'run_app_action'} <= set(offered)  # the rest still there
    assert 'GMAIL_SEND_EMAIL' not in offered
    built = of_type(events, ev.CONTEXT_BUILT)[0].payload
    assert built['disclosure'] == 'discovery' and built['recent_actions'] == ['GMAIL_FETCH_EMAILS']
    assert 'used recently are in your tool list' in model.instructions[0]
    # Called straight away: through the gate, logged as the real action
    assert [(e.payload['tool'], e.payload['allowed']) for e in of_type(events, ev.TOOL_CALL)] == \
        [('GMAIL_FETCH_EMAILS', True)]


async def test_run_app_action_takes_an_offered_action_without_describe(make_runtime):
    model = ScriptedModel([[call('1', 'run_app_action', {'action': 'GMAIL_FETCH_EMAILS', 'arguments': {}})],
                           [say('ok')]])
    rt = make_runtime(model)
    past_run(rt.store, [('GMAIL_FETCH_EMAILS', True)])
    _, events = await recent_run(rt, model)
    assert [e.payload.get('rejected_by') for e in of_type(events, ev.TOOL_CALL)] == [None]


async def test_old_denied_and_other_peoples_calls_dont_count(make_runtime):
    model = ScriptedModel([[say('Hi')]])
    rt = make_runtime(model)
    past_run(rt.store, [('GMAIL_SEND_EMAIL', True)], days_ago=8)  # older than a week
    past_run(rt.store, [('GMAIL_FETCH_EMAILS', False)])  # the gate refused it
    past_run(rt.store, [('SLACK_LIST_CHANNELS_1', True)], owner=False)  # not the owner's run
    past_run(rt.store, [('SLACK_LIST_CHANNELS_2', True)], days_ago=0)  # today: counts from tomorrow
    _, events = await recent_run(rt, model)
    assert 'recent_actions' not in of_type(events, ev.CONTEXT_BUILT)[0].payload
    assert not any(name.startswith(('GMAIL_', 'SLACK_')) for name in model.tool_names[0])
    assert 'Their actions are not listed here' in model.instructions[0]


async def test_the_cap_holds_most_used_first(make_runtime):
    model = ScriptedModel([[say('Hi')]])
    rt = make_runtime(model)
    calls = [(f'SLACK_LIST_CHANNELS_{i}', True) for i in range(13) for _ in range(i + 1)]  # 12 used most
    past_run(rt.store, calls)
    _, events = await recent_run(rt, model)
    recent = of_type(events, ev.CONTEXT_BUILT)[0].payload['recent_actions']
    assert recent == [f'SLACK_LIST_CHANNELS_{i}' for i in range(12, 2, -1)]  # 10, most used first
    assert {name for name in model.tool_names[0] if name.startswith('SLACK_')} == set(recent)


async def test_recent_actions_off_and_allowed_tools(make_runtime, home):
    model = ScriptedModel([[say('Hi')]])
    rt = make_runtime(model)
    past_run(rt.store, [('GMAIL_FETCH_EMAILS', True), ('GMAIL_SEND_EMAIL', True)])
    allowed = {f'SLACK_LIST_CHANNELS_{i}' for i in range(13)} | {'GMAIL_FETCH_EMAILS'}
    _, events = await recent_run(rt, model, allowed_tools=allowed)
    assert of_type(events, ev.CONTEXT_BUILT)[0].payload['recent_actions'] == ['GMAIL_FETCH_EMAILS']

    home.config_path.write_text('[mcp]\nrecent_actions = 0\n')
    model = ScriptedModel([[say('Hi')]])
    _, events = await recent_run(make_runtime(model), model)
    assert 'recent_actions' not in of_type(events, ev.CONTEXT_BUILT)[0].payload


async def test_a_non_owner_run_gets_no_recent_actions(make_runtime):
    model = ScriptedModel([[say('Hi')]])
    rt = make_runtime(model)
    past_run(rt.store, [('GMAIL_FETCH_EMAILS', True)])
    _, events = await recent_run(rt, model, owner=False)
    assert 'recent_actions' not in of_type(events, ev.CONTEXT_BUILT)[0].payload
    assert 'GMAIL_FETCH_EMAILS' not in model.tool_names[0]


async def test_recent_third_party_descriptions_taint_the_run(make_runtime, monkeypatch):
    from wraithd.core.gate import PermissionGate, ToolAccess
    from wraithd.mcp.client import Connected

    added = []
    original = PermissionGate.add_untrusted_input

    def spy(self, source, text, private=True):
        added.append((source, text, private))
        return original(self, source, text, private)

    monkeypatch.setattr(PermissionGate, 'add_untrusted_input', spy)
    tools = fake_app_tools([])
    mcp = Connected(tools=tools, access={tool.name: ToolAccess.READ for tool in tools},
                    origin={tool.name: ('apps', tool.name.lower()) for tool in tools})
    model = ScriptedModel([[say('Hi')]])
    rt = make_runtime(model)
    past_run(rt.store, [('GMAIL_FETCH_EMAILS', True)])
    _, events = await recent_run(rt, model, mcp=mcp)
    assert of_type(events, ev.CONTEXT_BUILT)[0].payload['untrusted_context'] == ['MCP tool descriptions']
    # Only the offered action's description, and as untrusted text, not the owner's private data
    assert added == [('MCP tool descriptions', ACTIONS['GMAIL_FETCH_EMAILS'][0], False)]


async def test_without_recent_actions_discovery_stays_untainted(make_runtime):
    tools = fake_app_tools([])
    from wraithd.core.gate import ToolAccess
    from wraithd.mcp.client import Connected
    mcp = Connected(tools=tools, access={tool.name: ToolAccess.READ for tool in tools},
                    origin={tool.name: ('apps', tool.name.lower()) for tool in tools})
    model = ScriptedModel([[say('Hi')]])
    _, events = await recent_run(make_runtime(model), model, mcp=mcp)
    assert of_type(events, ev.CONTEXT_BUILT)[0].payload['untrusted_context'] == []


def test_the_recent_set_is_the_same_all_day():
    from datetime import datetime

    from wraithd.store import SQLiteStore
    store = SQLiteStore()
    morning = datetime.now().astimezone().replace(hour=8, minute=0)
    names = set(ACTIONS)
    past_run(store, [('GMAIL_FETCH_EMAILS', True)])
    assert disclosure.recent_actions(store, names, now=morning) == ['GMAIL_FETCH_EMAILS']
    past_run(store, [('GMAIL_SEND_EMAIL', True)] * 3, days_ago=0)  # used today, more than the other
    evening = morning.replace(hour=23, minute=59)
    assert disclosure.recent_actions(store, names, now=evening) == ['GMAIL_FETCH_EMAILS']
    assert disclosure.recent_actions(store, names, limit=0, now=evening) == []
