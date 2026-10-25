"""
Progressive disclosure of app actions: with many actions, the model gets find -> describe -> run instead of every
schema, the order is enforced in code, and every real action still goes through the permission gate and the
event log.
"""

import json

from agents import FunctionTool

from local_agent.core import disclosure
from local_agent.core import events as ev
from local_agent.core.gate import guard_function_tools
from tests.scripted import ScriptedModel, call, run_agent, say

SEND_SCHEMA = {'type': 'object', 'properties': {
    'recipient_email': {'type': 'string', 'description': 'Who gets the email'},
    'subject': {'type': 'string'},
    'body': {'type': 'string'},
    'is_html': {'type': 'boolean', 'default': False},
}, 'required': ['recipient_email', 'body']}
FETCH_SCHEMA = {'type': 'object', 'properties': {'query': {'type': 'string', 'description': 'Gmail search'}}}

# 15 actions: more than DIRECT_ACTIONS_LIMIT, so a run would use discovery
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


async def discovery_run(store, turns, message, app_calls, callback=None):
    """A run where the app actions stay out of the tool list, as the real run() does past the limit."""
    def wrap(tools, gate, log, after_result):
        catalog = disclosure.AppCatalog(guard_function_tools(fake_app_tools(app_calls), gate, log, after_result),
                                        guides=GUIDES)
        find_tool, describe_tool, run_tool = disclosure.discovery_tools(catalog, log)
        return guard_function_tools(tools + [find_tool, describe_tool], gate, log, after_result) + [run_tool]

    model = ScriptedModel(turns)
    _, log = await run_agent(model, [], message, store=store, callback=callback, wrap=wrap)
    return model, log


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

async def test_many_actions_are_discovered_not_listed(store):
    app_calls = []
    model, log = await discovery_run(store, [
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
    assert [e.payload['tool'] for e in log.of_type(ev.TOOL_CALL)] == [
        'find_app_actions', 'describe_app_action', 'GMAIL_FETCH_EMAILS', 'describe_app_action']


async def test_an_action_must_be_described_before_it_runs(store):
    app_calls = []
    model, log = await discovery_run(store, [
        [call('1', 'run_app_action', {'action': 'GMAIL_FETCH_EMAILS', 'arguments': {}})],
        [say('I need to look it up first.')],
    ], 'fetch my mail', app_calls)

    assert app_calls == []
    assert 'has not been described' in json.loads(model.tool_outputs(1)['call_1'])['error']
    result = log.of_type(ev.TOOL_RESULT)[0].payload
    assert result['tool'] == 'GMAIL_FETCH_EMAILS' and result['outcome'] == 'error'  # counts toward stop conditions


async def test_missing_arguments_are_caught_before_the_app_is_called(store):
    app_calls = []
    model, _ = await discovery_run(store, [
        [call('1', 'describe_app_action', {'action': 'GMAIL_SEND_EMAIL'})],
        [call('2', 'run_app_action', {'action': 'GMAIL_SEND_EMAIL', 'arguments': {'subject': 'Hi', 'cc': 'x'}})],
        [say('Who should I send it to?')],
    ], 'send an email', app_calls)

    error = json.loads(model.tool_outputs(2)['call_2'])['error']
    assert 'missing required arguments: recipient_email, body' in error and 'unknown arguments: cc' in error
    assert app_calls == []


async def test_an_unknown_action_gets_suggestions(store):
    model, _ = await discovery_run(store, [
        [call('1', 'describe_app_action', {'action': 'gmail_send_emails'})],
        [say('Found it.')],
    ], 'send an email', [])
    described = json.loads(model.tool_outputs(1)['call_1'])
    assert described['did_you_mean'][0] == 'GMAIL_SEND_EMAIL'


async def test_a_write_through_discovery_still_needs_approval(store):
    app_calls = []
    model, log = await discovery_run(store, [
        [call('1', 'describe_app_action', {'action': 'GMAIL_SEND_EMAIL'})],
        [call('2', 'run_app_action', {'action': 'GMAIL_SEND_EMAIL',
                                      'arguments': {'recipient_email': 'dana@co.com', 'body': 'Hi'}})],
        [say('This needs your approval.')],
    ], 'email dana', app_calls)  # no callback: nobody is there to approve

    assert app_calls == []
    assert 'needs the user\'s approval' in json.loads(model.tool_outputs(2)['call_2'])['error']
    send = [e.payload for e in log.of_type(ev.TOOL_CALL) if e.payload['tool'] == 'GMAIL_SEND_EMAIL'][0]
    assert send['access'] == 'write' and send['allowed'] is False
