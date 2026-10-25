"""
Security in the harness: outbound requests can't reach the machine's own network, secrets don't reach the
model or the log, and budgets stop runaway runs.
"""

import json
import socket

import httpx
import pytest
from agents.usage import Usage

from local_agent.core import egress, secrets
from local_agent.core import events as ev
from local_agent.core.stop import StopPolicy
from tests.scripted import ScriptedModel, call, fake_tool, run_agent, say

OPENAI_LIKE = 'sk-proj-' + 'a1B2' * 10


# --- egress ------------------------------------------------------------------------------------------------

@pytest.fixture
def dns(monkeypatch):
    """Resolves made-up hostnames to the addresses a test gives them."""
    table = {}

    def getaddrinfo(host, port, *args, **kwargs):
        if host not in table:
            raise socket.gaierror('unknown host')
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, port)) for address in table[host]]

    monkeypatch.setattr(egress.socket, 'getaddrinfo', getaddrinfo)
    return table


@pytest.mark.parametrize('address, public', [
    ('93.184.216.34', True), ('2606:4700::1111', True),
    ('127.0.0.1', False), ('10.1.2.3', False), ('172.16.0.1', False), ('192.168.1.1', False),
    ('169.254.169.254', False),  # link-local, and the cloud metadata service
    ('100.64.0.1', False), ('0.0.0.0', False), ('::1', False), ('fd00::1', False), ('fe80::1%en0', False),
    ('::ffff:127.0.0.1', False), ('224.0.0.1', False),
])
def test_only_public_addresses_count_as_public(address, public):
    assert egress.is_public(address) is public


@pytest.mark.parametrize('url, reason', [
    ('file:///etc/passwd', 'Only http and https'),
    ('ftp://example.com/x', 'Only http and https'),
    ('http://admin:pw@example.com/', 'username or password'),
    ('http://example.com:8080/', 'non-standard ports'),
    ('http://localhost:11434/api/tags', 'non-standard ports'),  # Ollama on the same laptop
    ('http://localhost/', 'private or internal'),
    ('http://router.lan/', 'private or internal'),  # the home network
    ('http://metadata.internal/latest/meta-data/', 'private or internal'),
    ('http://rebind.example/', 'private or internal'),  # one public and one private answer
    ('http://nowhere.example/', 'could not be found'),
])
def test_urls_that_reach_inside_are_blocked(dns, url, reason):
    dns.update({'example.com': ['93.184.216.34'], 'localhost': ['127.0.0.1'], 'router.lan': ['192.168.1.1'],
                'metadata.internal': ['169.254.169.254'], 'rebind.example': ['93.184.216.34', '10.0.0.5']})
    with pytest.raises(egress.EgressBlocked, match=reason):
        egress.check_url(url)


class FakeStream:
    """What httpx exposes about the connection: here, only the peer address."""

    def __init__(self, peer):
        self.peer = peer

    def get_extra_info(self, name):
        return (self.peer, 443) if name == 'server_addr' else None


@pytest.fixture
def web(dns):
    """The responses the fake web returns, in order, and the URLs requested. Returns (queue, requested, transport)."""
    dns.update({'example.com': ['93.184.216.34'], 'other.com': ['93.184.216.35']})
    queue, requested = [], []

    def handler(request):
        requested.append(str(request.url))
        status, headers, body, peer = queue.pop(0)
        return httpx.Response(status, headers=headers, content=body, extensions={'network_stream': FakeStream(peer)})

    return queue, requested, httpx.MockTransport(handler)


def page(status=200, headers=None, body=b'<p>ok</p>', peer='93.184.216.34'):
    return status, headers or {}, body, peer


def test_a_redirect_to_the_metadata_service_is_blocked(web):
    queue, requested, transport = web
    queue.append(page(302, {'location': 'http://169.254.169.254/latest/meta-data/iam/'}))
    with pytest.raises(egress.EgressBlocked, match='private or internal'):
        egress.safe_get('https://example.com/innocent', transport=transport)
    assert requested == ['https://example.com/innocent']  # the internal address was never requested


def test_a_redirect_to_localhost_is_blocked(web):
    queue, requested, transport = web
    queue.append(page(307, {'location': 'http://127.0.0.1/admin'}))
    with pytest.raises(egress.EgressBlocked, match='private or internal'):
        egress.safe_get('https://example.com/', transport=transport)
    assert requested == ['https://example.com/']


def test_redirects_are_followed_and_checked(web):
    queue, _, transport = web
    queue += [page(301, {'location': 'https://other.com/page'}), page()]
    fetched = egress.safe_get('https://example.com/old', transport=transport)
    assert fetched.url == 'https://other.com/page' and fetched.content == b'<p>ok</p>'


def test_too_many_redirects_stop(web):
    queue, _, transport = web
    queue += [page(302, {'location': 'https://example.com/again'})] * (egress.MAX_REDIRECTS + 1)
    with pytest.raises(egress.EgressBlocked, match='redirected more than'):
        egress.safe_get('https://example.com/', transport=transport)


def test_a_private_peer_after_connecting_is_blocked(web):
    queue, _, transport = web
    queue.append(page(peer='10.0.0.5'))  # DNS changed between check and connect
    with pytest.raises(egress.EgressBlocked):
        egress.safe_get('https://example.com/', transport=transport)


def test_big_pages_are_cut_at_the_cap(web, monkeypatch):
    queue, _, transport = web
    monkeypatch.setattr(egress, 'MAX_BYTES', 100)
    queue.append(page(body=b'x' * 60 + b'y' * 60 + b'z' * 60))
    fetched = egress.safe_get('https://example.com/', transport=transport)
    assert len(fetched.content) == 100 and fetched.truncated


# --- secrets -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize('text, kind', [
    (f'key: {OPENAI_LIKE}', 'openai_key'),
    ('sk-ant-api03-' + 'x' * 30, 'anthropic_key'),
    ('ghp_' + 'A' * 36, 'github_token'),
    ('xoxb-1234567890-abcdefghij', 'slack_token'),
    ('AKIAIOSFODNN7EXAMPLE', 'aws_access_key'),
    ('sk_live_' + '4' * 24, 'stripe_key'),
    ('AIza' + 'B' * 35, 'google_api_key'),
    ('Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456', 'bearer_token'),
    ('eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV', 'jwt'),
    ('-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\n-----END RSA PRIVATE KEY-----', 'private_key'),
    ('password = hunter2hunter2', 'credential'),
])
def test_token_shapes_are_redacted(text, kind):
    redacted, kinds = secrets.redact(text)
    assert kind in kinds and f'[REDACTED:{kind}]' in redacted


def test_ordinary_text_is_left_alone():
    text = 'Meeting at 10:30 with Dana, invoice #4512, code 839201, see https://example.com/a?b=c'
    assert secrets.redact(text) == (text, [])


def test_the_owners_own_keys_are_redacted_by_value(monkeypatch):
    secrets.register_secret('plain-looking-mcp-token-123')
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', '123456:plain-telegram-token')
    secrets.register_secret('short')  # too short to redact safely: ignored
    text = 'token plain-looking-mcp-token-123 and 123456:plain-telegram-token, short'
    assert secrets.redact(text) == ('token [REDACTED:owner_secret] and [REDACTED:owner_secret], short',
                                    ['owner_secret'])


async def test_a_secret_in_a_tool_result_never_reaches_the_model_or_the_log(store):
    model = ScriptedModel([[call('1', 'web_search', {'query': 'my key'})], [say('done')]])
    _, log = await run_agent(model, [fake_tool('web_search', lambda **a: f'Your key is {OPENAI_LIKE}')], 'find my key',
                             store=store)

    seen = model.tool_outputs(1)['call_1']
    assert OPENAI_LIKE not in seen and '[REDACTED:openai_key]' in seen
    [result] = [e for e in store.run_events(log.run_id) if e.type == ev.TOOL_RESULT]
    assert result.payload['redacted'] == ['openai_key']
    assert OPENAI_LIKE not in json.dumps([e.payload for e in store.run_events(log.run_id)])


def test_secrets_in_arguments_are_redacted_before_storage():
    log = ev.EventLog()
    event = log.append(ev.TOOL_CALL, tool='memory_append', arguments={'text': f'API key {OPENAI_LIKE}'})
    assert OPENAI_LIKE not in json.dumps(event.payload)
    assert '[REDACTED:openai_key]' in event.payload['arguments']['text']


# --- budgets -----------------------------------------------------------------------------------------------

async def test_a_run_stops_at_its_tool_call_limit(store):
    model = ScriptedModel([[call('1', 'calculate', {'expression': '1+1'})],
                           [call('2', 'calculate', {'expression': '2+2'})],
                           [say('Stopped after two.')]])
    answer, _ = await run_agent(model, [fake_tool('calculate', lambda **a: 1)], 'calculate a lot', store=store,
                                policy=StopPolicy(max_tool_calls=2))
    assert answer == 'Stopped after two.'
    assert model.tool_names[-1] == [] and 'its limit' in model.inputs[-1][-1]['content']


async def test_token_usage_is_logged_and_the_run_budget_stops_the_run(store):
    usage = Usage(requests=1, input_tokens=600, output_tokens=50, total_tokens=650)
    model = ScriptedModel([[call('1', 'calculate', {'expression': '1+1'})],
                           [call('2', 'calculate', {'expression': '2+2'})],
                           [say('Over budget, stopping.')]], usage=usage)
    answer, log = await run_agent(model, [fake_tool('calculate', lambda **a: 1)], 'calculate a lot', store=store,
                                  policy=StopPolicy(token_budget=1000))
    assert answer == 'Over budget, stopping.'
    assert [e.payload['input_tokens'] for e in log.of_type(ev.MODEL_OUTPUT)][:2] == [600, 600]
    assert log.of_type(ev.STOP)[0].payload['reason'] == 'token_budget'
    assert len(model.inputs) == 3  # two calls used 1300 tokens: the third is the final answer, without tools
