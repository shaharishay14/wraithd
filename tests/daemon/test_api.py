"""
The local API against docs/api.md: the chat stream (with an approval decided through the API), conversations,
runs, memory, skills, schedules, MCP servers, Telegram pairing, errors, and serving the UI.
"""

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from local_agent import cli, schedules
from local_agent.core import gate as gate_module
from local_agent.memory import vault
from local_agent.memory.notes import SOURCE_AGENT, Note
from local_agent.skills import registry
from tests.scripted import ScriptedModel, call, say

FAKE = str(Path(__file__).parent.parent / 'fixtures' / 'fake_mcp.py')


def events_of(body: str) -> list[tuple[str, dict]]:
    """A server-sent event stream as (event, data) pairs."""
    found = []
    for block in body.strip().split('\n\n'):
        lines = dict(line.split(': ', 1) for line in block.splitlines())
        found.append((lines['event'], json.loads(lines['data'])))
    return found


async def wait_for_pending(rt, seconds=5.0):
    for _ in range(int(seconds / 0.01)):
        pending = rt.store.list_approvals()
        if pending:
            return pending[0]
        await asyncio.sleep(0.01)
    raise AssertionError('no approval was asked for')


@pytest.fixture(autouse=True)
def quick_polls(monkeypatch):
    monkeypatch.setattr(gate_module, 'APPROVAL_POLL_SECONDS', 0.01)


# --- chat --------------------------------------------------------------------------------------------------

async def test_chat_streams_the_answer_and_keeps_the_conversation(make_daemon):
    daemon, client = await make_daemon(ScriptedModel([[say('Hello from the web.')], [say('Still here.')]]))
    response = await client.post('/api/chat', json={'message': 'hi'})
    assert response.status_code == 200 and response.headers['content-type'].startswith('text/event-stream')
    events = events_of(response.text)
    assert events[0][0] == 'start' and events[0][1]['run_id']
    assert ''.join(data['content'] for event, data in events if event == 'delta') == 'Hello from the web.'
    assert events[-1] == ('done', {'run_id': events[0][1]['run_id'], 'outcome': 'answered',
                                   'answer': 'Hello from the web.'})

    conversation_id = events[0][1]['conversation_id']
    again = events_of((await client.post('/api/chat', json={'message': 'still there?',
                                                            'conversation_id': conversation_id})).text)
    assert again[-1][1]['answer'] == 'Still here.'
    [listed] = (await client.get('/api/conversations')).json()
    assert listed['id'] == conversation_id and listed['source'] == 'web' and listed['preview'] == 'hi'
    messages = (await client.get(f'/api/conversations/{conversation_id}/messages')).json()
    assert [(m['role'], m['content']) for m in messages] == [('user', 'hi'), ('assistant', 'Hello from the web.'),
                                                             ('user', 'still there?'), ('assistant', 'Still here.')]


async def test_an_approval_in_the_web_chat_is_decided_through_the_api(make_daemon, home):
    model = ScriptedModel([[call('1', 'skill_adjust', {'skill': 'weekly-review', 'rule': 'Five lines at most'})],
                           [say('Saved: five lines at most.')]])
    daemon, client = await make_daemon(model)
    stream = asyncio.create_task(client.post('/api/chat', json={'message': 'keep weekly reviews to 5 lines, always'}))

    approval = await wait_for_pending(daemon.rt)
    [card] = (await client.get('/api/approvals')).json()
    assert card['tool'] == 'skill_adjust' and card['arguments'] == {'skill': 'weekly-review',
                                                                    'rule': 'Five lines at most'}
    assert 'every time it uses this skill' in card['reason'] and card['channel'] == 'chat' and card['run_id']
    assert card['status'] == 'pending' and card['expires_at'] > card['created_at']
    # while it waits, the conversation is busy
    conversation_id = daemon.rt.store.list_conversations()[0].id
    busy = await client.post('/api/chat', json={'message': 'hello?', 'conversation_id': conversation_id})
    assert busy.status_code == 409 and busy.json()['error']['code'] == 'conflict'

    decided = await client.post(f'/api/approvals/{approval.id}', json={'decision': 'approve'})
    assert decided.json()['status'] == 'approved'
    events = events_of((await stream).text)
    assert [event for event, _ in events if event != 'delta'] == ['start', 'approval_required', 'approval_resolved',
                                                                   'tool', 'done']
    assert ('approval_resolved', {'approval_id': approval.id, 'status': 'approved'}) in events
    assert next(data for event, data in events if event == 'approval_required')['approval_id'] == approval.id
    assert registry.SkillFolder(home.skills_dir).adjustments() == {'weekly-review': ['Five lines at most']}
    # deciding again is a conflict, with the current status in the message
    again = await client.post(f'/api/approvals/{approval.id}', json={'decision': 'deny'})
    assert again.status_code == 409 and 'approved' in again.json()['error']['message']


async def test_chat_errors(make_daemon):
    _, client = await make_daemon(ScriptedModel([]))
    assert (await client.post('/api/chat', json={'message': ''})).json()['error']['code'] == 'bad_request'
    assert (await client.post('/api/chat', content=b'{nope')).status_code == 400
    missing = await client.post('/api/chat', json={'message': 'hi', 'conversation_id': 'nope'})
    assert missing.status_code == 404
    assert (await client.post('/api/approvals/nope', json={'decision': 'maybe'})).status_code == 400
    assert (await client.post('/api/approvals/nope', json={'decision': 'approve'})).status_code == 404
    unknown = await client.get('/api/nothing-here')
    assert unknown.status_code == 404 and unknown.json()['error']['code'] == 'not_found'


# --- runs --------------------------------------------------------------------------------------------------

async def test_runs_and_one_runs_timeline(make_daemon):
    _, client = await make_daemon(ScriptedModel([[say('Hi.')]]))
    run_id = events_of((await client.post('/api/chat', json={'message': 'hi'})).text)[0][1]['run_id']
    [listed] = (await client.get('/api/runs')).json()
    assert listed['run_id'] == run_id and listed['source'] == 'chat' and listed['outcome'] == 'answered'
    assert set(listed) == {'run_id', 'started_at', 'source', 'message', 'outcome', 'turns', 'tool_calls', 'tokens',
                           'stop_reason', 'untrusted_sources'}
    detail = (await client.get(f'/api/runs/{run_id}')).json()
    assert detail['summary']['outcome'] == 'answered' and detail['summary']['disclosure']['mode'] == 'direct'
    assert [e['type'] for e in detail['events']][:2] == ['run_start', 'context_built']
    assert (await client.get('/api/runs/nope')).status_code == 404
    assert (await client.get('/api/runs?limit=x')).status_code == 400


# --- memory ------------------------------------------------------------------------------------------------

async def test_memory_list_read_write_and_vouch(make_daemon, home):
    daemon, client = await make_daemon(ScriptedModel([]))
    daemon.rt.memory.save(Note(path='people/eve.md', title='Eve', body='Planted', trust=vault.LOW,
                               source=SOURCE_AGENT, source_detail='run 1, after reading web_search'))
    listing = (await client.get('/api/memory')).json()
    assert set(listing['folders']) == set(vault.FOLDERS) and listing['notes'][0]['trust'] == 'low'
    eve = (await client.get('/api/memory/people/eve.md')).json()
    assert eve['trust'] == 'low' and 'after reading web_search' in eve['source_detail']

    saved = (await client.put('/api/memory/people/eve.md', json={'body': 'My accountant.'})).json()
    assert (saved['trust'], saved['source'], saved['body']) == ('high', 'user', 'My accountant.')  # vouched
    assert 'trust: low' not in (home.memory_dir / 'people' / 'eve.md').read_text()
    created = (await client.put('/api/memory/people/dana.md', json={'body': 'Works on [[eve]].', 'title': 'Dana'}))
    assert created.json()['links'] == ['eve']
    assert (await client.get('/api/memory/people/eve.md')).json()['linked_from'] == ['people/dana.md']

    assert (await client.delete('/api/memory/people/eve.md')).status_code == 204
    assert (await client.get('/api/memory/people/eve.md')).status_code == 404
    assert (await client.put('/api/memory/secrets/keys.md', json={'body': 'x'})).status_code == 422
    assert (await client.put('/api/memory/people/x.md', json={'body': 3})).status_code == 400


# --- skills ------------------------------------------------------------------------------------------------

async def test_skills_availability_rules_and_proposals(make_daemon, home):
    daemon, client = await make_daemon(ScriptedModel([]))
    skills = {s['name']: s for s in (await client.get('/api/skills')).json()}
    assert skills['morning-brief']['available'] is False and skills['morning-brief']['missing'] == ['email']
    assert skills['weekly-review']['available'] is True and skills['inbox-triage']['model'] == 'strong'

    rules = await client.put('/api/skills/weekly-review/adjustments', json={'rules': ['Five lines', '  No   fluff ']})
    assert rules.json()['adjustments'] == ['Five lines', 'No fluff'] and 'instructions' in rules.json()
    assert (await client.put('/api/skills/weekly-review/adjustments', json={'rules': ['a\nb']})).status_code == 400
    assert (await client.put('/api/skills/weekly-review/adjustments', json={'rules': []})).json()['adjustments'] == []
    assert (await client.get('/api/skills/nope')).status_code == 404

    folder = registry.SkillFolder(home.skills_dir)
    folder.propose('sales-digest', 'Sum up sales.', '1. Read. 2. Sum.', [], 'run 7')
    [proposal] = (await client.get('/api/skills/proposals')).json()
    assert proposal == {'name': 'sales-digest', 'description': 'Sum up sales.', 'instructions': '1. Read. 2. Sum.',
                        'requires': [], 'proposed_by': 'run 7'}
    approved = await client.post('/api/skills/proposals/sales-digest/approve')
    assert approved.json()['source'] == 'user' and folder.is_active('sales-digest')
    assert (await client.post('/api/skills/proposals/sales-digest/approve')).status_code == 404
    folder.propose('sales-digest', 'Again.', 'x', [], 'run 8')
    assert (await client.post('/api/skills/proposals/sales-digest/approve')).status_code == 409
    assert (await client.delete('/api/skills/proposals/sales-digest')).status_code == 204
    assert (await client.get('/api/skills/proposals')).json() == []


# --- schedules ---------------------------------------------------------------------------------------------

async def test_schedules_list_and_cancel(make_daemon):
    daemon, client = await make_daemon(ScriptedModel([]))
    created = schedules.create(daemon.rt.store, 'Morning brief', 'Europe/London', datetime.now(UTC),
                               cron='0 8 * * 1-5')
    [listed] = (await client.get('/api/schedules')).json()
    assert listed['id'] == created.id and listed['cron'] == '0 8 * * 1-5' and listed['when'].startswith('every:')
    assert (await client.delete(f'/api/schedules/{created.id}')).status_code == 204
    assert (await client.get('/api/schedules')).json() == []
    assert (await client.get('/api/schedules?all=true')).json()[0]['active'] is False
    assert (await client.delete(f'/api/schedules/{created.id}')).status_code == 404


# --- MCP ---------------------------------------------------------------------------------------------------

async def test_mcp_servers_tools_and_always_allow(make_daemon, home, capsys):
    cli.main(['mcp', 'add', 'gmail', '--provides', 'email', '--env', 'GMAIL_TOKEN=gmail-token-1234567890', '--',
              sys.executable, FAKE, '--app', 'gmail'])
    daemon, client = await make_daemon(ScriptedModel([]))
    [server] = (await client.get('/api/mcp/servers')).json()
    assert server['connected'] is True and server['secrets'] == ['GMAIL_TOKEN'] and server['provides'] == ['email']
    assert 'gmail-token' not in json.dumps(server)
    tools = {t['tool']: t for t in (await client.get('/api/mcp/servers/gmail/tools')).json()}
    assert tools['fetch_emails']['tier'] == 'read-only' and tools['send_email']['tier'] == 'asks'
    assert tools['send_email']['registered_name'] == 'GMAIL_SEND_EMAIL'
    # skills now see email
    assert {s['name']: s for s in (await client.get('/api/skills')).json()}['morning-brief']['available'] is True

    allowed = await client.put('/api/mcp/servers/gmail/always-allow/send_email')
    assert allowed.json()['always_allow'] == ['send_email'] and 'GMAIL_SEND_EMAIL' in daemon.mcp.always_allowed
    assert {t['tool']: t['tier'] for t in (await client.get('/api/mcp/servers/gmail/tools')).json()}[
        'send_email'] == 'always allowed'
    assert 'always_allow = ["send_email"]' in home.config_path.read_text()
    await client.delete('/api/mcp/servers/gmail/always-allow/send_email')
    assert 'GMAIL_SEND_EMAIL' not in daemon.mcp.always_allowed
    assert (await client.get('/api/mcp/servers/nope/tools')).status_code == 404


# --- status, Telegram --------------------------------------------------------------------------------------

async def test_status_and_telegram_without_a_bot(make_daemon):
    _, client = await make_daemon(ScriptedModel([]))
    status = (await client.get('/api/status')).json()
    assert status['model'] == {'provider': 'openai', 'default': 'gpt-4o-mini', 'strong': 'gpt-4o'}
    assert status['telegram'] == {'configured': False, 'paired': False, 'owner': None} and status['mcp'] == []
    assert status['scheduler'] == {'running': False, 'next': None}
    pairing = await client.post('/api/telegram/pairing')
    assert pairing.status_code == 409 and 'key set telegram' in pairing.json()['error']['message']


# --- the UI ------------------------------------------------------------------------------------------------

async def test_without_a_build_a_placeholder_is_served(make_daemon):
    _, client = await make_daemon(ScriptedModel([]))
    response = await client.get('/', headers={'Authorization': ''})  # static files need no token
    assert response.status_code == 200 and 'Local Agent is running' in response.text


async def test_the_built_ui_is_served_with_an_index_fallback(make_daemon, tmp_path, monkeypatch):
    ui = tmp_path / 'dist'
    (ui / 'assets').mkdir(parents=True)
    (ui / 'index.html').write_text('<div id="app"></div>')
    (ui / 'assets' / 'app.js').write_text('console.log(1)')
    (tmp_path / 'secret.txt').write_text('not for the browser')
    monkeypatch.setenv('LOCAL_AGENT_UI_DIR', str(ui))
    _, client = await make_daemon(ScriptedModel([]))
    assert (await client.get('/assets/app.js')).text == 'console.log(1)'
    assert (await client.get('/runs/r1')).text == '<div id="app"></div>'  # the app's own route
    assert (await client.get('/assets/..%2F..%2Fsecret.txt')).text == '<div id="app"></div>'  # never outside


# --- contract v1.1 -----------------------------------------------------------------------------------------

async def test_a_decision_from_another_channel_reaches_the_open_stream(make_daemon):
    """Decided outside this request (a Telegram button writes to the same store), or denied: the stream hears it."""
    model = ScriptedModel([[call('1', 'skill_adjust', {'skill': 'weekly-review', 'rule': 'Short'})],
                           [say('Not saved.')]])
    daemon, client = await make_daemon(model)
    stream = asyncio.create_task(client.post('/api/chat', json={'message': 'keep weekly reviews short, always'}))
    approval = await wait_for_pending(daemon.rt)
    daemon.rt.store.decide_approval(approval.id, 'denied')  # what TelegramChannel does on a Deny tap
    events = events_of((await stream).text)
    assert ('approval_resolved', {'approval_id': approval.id, 'status': 'denied'}) in events
    assert events[-1][0] == 'done'


async def test_an_expired_card_is_resolved_on_the_stream(make_daemon, monkeypatch):
    monkeypatch.setattr(gate_module, 'APPROVAL_TIMEOUT_SECONDS', 0.1)
    model = ScriptedModel([[call('1', 'skill_adjust', {'skill': 'weekly-review', 'rule': 'Short'})], [say('Ok.')]])
    _, client = await make_daemon(model)
    events = events_of((await client.post('/api/chat', json={'message': 'keep it short, always'})).text)
    assert next(data for event, data in events if event == 'approval_resolved')['status'] == 'expired'


async def test_approval_history_in_one_call(make_daemon):
    daemon, client = await make_daemon(ScriptedModel([]))
    store = daemon.rt.store
    pending = store.create_approval('skill_adjust', {'rule': 'a'})
    approved = store.create_approval('skill_adjust', {'rule': 'b'})
    store.decide_approval(approved.id, 'approved')
    store.create_approval('GMAIL_SEND_EMAIL', {'to': 'x'}, status='preapproved')

    def ids(response):
        return [card['approval_id'] for card in response.json()]
    assert ids(await client.get('/api/approvals')) == [pending.id]
    assert len((await client.get('/api/approvals?status=all')).json()) == 3
    assert ids(await client.get('/api/approvals?status=pending,approved')) == [approved.id, pending.id]
    for bad in ('nope', 'pending,nope', ','):
        assert (await client.get(f'/api/approvals?status={bad}')).status_code == 400


async def test_assistant_messages_link_to_their_run(make_daemon):
    _, client = await make_daemon(ScriptedModel([[say('Hi.')]]))
    events = events_of((await client.post('/api/chat', json={'message': 'hi'})).text)
    conversation_id, run_id = events[0][1]['conversation_id'], events[0][1]['run_id']
    messages = (await client.get(f'/api/conversations/{conversation_id}/messages')).json()
    assert [(m['role'], m['run_id']) for m in messages] == [('user', None), ('assistant', run_id)]


async def test_tools_of_a_server_that_is_not_connected_is_a_409(make_daemon, capsys):
    cli.main(['mcp', 'add', 'broken', '--', sys.executable, '-c', 'raise SystemExit(1)'])
    _, client = await make_daemon(ScriptedModel([]))
    response = await client.get('/api/mcp/servers/broken/tools')
    assert response.status_code == 409 and response.json()['error']['code'] == 'conflict'
    assert response.json()['error']['message'].startswith('broken is not connected')
    [server] = (await client.get('/api/mcp/servers')).json()
    assert server['connected'] is False and server['error']


async def test_put_on_a_new_memory_path_creates_the_note(make_daemon, home):
    _, client = await make_daemon(ScriptedModel([]))
    assert (await client.get('/api/memory/projects/garden.md')).status_code == 404
    created = await client.put('/api/memory/projects/garden.md', json={'body': 'Plant tomatoes in April.'})
    assert created.status_code == 200 and created.json()['title'] == 'Garden' and created.json()['trust'] == 'high'
    assert (home.memory_dir / 'projects' / 'garden.md').exists()
