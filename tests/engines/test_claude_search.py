"""
[web] search = "claude": wraithd's web_search runs a separate one-shot Claude Code whose only tool is WebSearch, on
the owner's login. Offline: claude_agent_sdk is the fake (fake_sdk.py), so no Claude Code starts and no model or
search is called.
"""

import json
from dataclasses import dataclass

import pytest

from tests.engines.fake_sdk import AssistantMessage, SystemMessage, TextBlock, call, init, result
from tests.engines.test_claude_engine import claude_rt  # noqa: F401 (a fixture)
from wraithd.config import Config, ModelConfig, set_values
from wraithd.core import events as ev
from wraithd.core import views
from wraithd.engines import claude_search
from wraithd.run import run
from wraithd.tools import choose_search, search_provider
from wraithd.tools.web import ClaudeSearch, SearchFailed

ENGINE = {'name': 'claude-code', 'chat_model': 'sonnet', 'background_model': 'haiku'}
ROWS = [{'title': 'Tram 28 - Carris', 'url': 'https://www.carris.pt/28', 'snippet': 'Tram 28 runs every 10 minutes.'},
        {'title': 'Trams', 'url': 'https://visitlisboa.com/trams', 'snippet': 'Tickets cost 3 euros on board.'}]
USAGE = {'input_tokens': 900, 'output_tokens': 150, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0,
         'server_tool_use': {'web_search_requests': 1}}


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict


def searched(answer=None, tools=('WebSearch',), usage=USAGE):
    """The helper's script: init (offering only WebSearch), one WebSearch call, the JSON answer."""
    return [SystemMessage('init', {'apiKeySource': 'none', 'tools': list(tools)}),
            AssistantMessage([ToolUseBlock('t1', 'WebSearch', {'query': 'lisbon trams'})]),
            AssistantMessage([TextBlock('done')]),
            result(json.dumps(ROWS) if answer is None else answer, usage=usage)]


async def test_the_helper_gets_only_websearch_on_the_login_and_the_background_model(claude_sdk):
    fake = claude_sdk(searched())
    found = await claude_search.search('lisbon trams', 5, ENGINE)
    assert found.rows == ROWS and found.searches == 1 and (found.input_tokens, found.output_tokens) == (900, 150)
    options = fake.calls[0]['options']
    assert options.tools == ['WebSearch'] and options.allowed_tools == ['WebSearch']
    assert options.mcp_servers == {} and options.strict_mcp_config is True and options.setting_sources == []
    assert options.env == {'ANTHROPIC_API_KEY': ''} and 'no-session-persistence' in options.extra_args
    assert options.model == 'haiku' and options.max_turns <= 5
    assert 'lisbon trams' in fake.calls[0]['prompt'] and 'JSON' in fake.calls[0]['prompt']


@pytest.mark.parametrize('answer, rows', [
    ('Here you go:\n```json\n' + json.dumps(ROWS) + '\n```', ROWS),
    (json.dumps(ROWS + [{'title': 'x', 'url': 'javascript:alert(1)'}, 'junk', {'url': 'file:///etc/passwd'}]), ROWS),
    ('I could not find anything.', []),
    ('[not json', []),
])
def test_rows_are_parsed_from_the_answer(answer, rows):
    assert claude_search.parse(answer, 5) == rows


def test_count_and_lengths_are_capped():
    many = [{'title': 't' * 500, 'url': f'https://e.example/{i}', 'snippet': 's' * 2000} for i in range(9)]
    rows = claude_search.parse(json.dumps(many), 3)
    assert len(rows) == 3 and len(rows[0]['title']) == 200 and len(rows[0]['snippet']) == claude_search.SNIPPET_CHARS


async def test_a_helper_offered_any_other_tool_stops(claude_sdk):
    claude_sdk(searched(tools=('WebSearch', 'WebFetch')))
    with pytest.raises(claude_search.SearchError, match='other than WebSearch'):
        await claude_search.search('q', 5, ENGINE)


@pytest.mark.parametrize('script, message', [
    ([init('ANTHROPIC_API_KEY'), result('[]')], 'unavailable: Claude Code is not using your Claude login'),
    ([init(), result(None, is_error=True, api_error_status=429)], 'usage limit'),
    ([init(), result(None, is_error=True, api_error_status=401)], "isn't signed in"),
    ([init(), result(None, is_error=True, api_error_status=500)], 'Web search failed.'),
    ([init()], 'Web search got no answer.'),
])
async def test_failures_are_a_plain_search_error(claude_sdk, script, message):
    claude_sdk(script)
    with pytest.raises(SearchFailed, match=message):
        await ClaudeSearch(ENGINE).search('q', 5)


async def test_a_missing_extra_says_how_to_install_it(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, 'claude_agent_sdk', None)  # import fails, as without the extra
    with pytest.raises(SearchFailed, match=r"wraithd\[claude\]"):
        await ClaudeSearch(ENGINE).search('q', 5)


def test_search_claude_is_chosen_and_says_what_it_uses(claude_sdk):
    claude_sdk()
    config = Config(model=ModelConfig(provider='openai'), search='claude')
    choice = choose_search(config)
    assert choice.backend == 'claude' and 'Claude subscription' in choice.why
    assert isinstance(search_provider(config), ClaudeSearch)


async def test_a_chat_searches_through_the_subscription_tainted_logged_and_counted(claude_rt, claude_sdk,  # noqa: F811
                                                                                   home):
    """The engine calls wraithd's web_search; the helper's rows come back as third-party text, and its tokens count."""
    searching = call('web_search', {'query': 'lisbon trams'})
    fake = claude_sdk([init(), searching, result('Tram 28 every 10 minutes.')], searched())
    set_values(home, 'web', {'search': 'claude'})
    rt = claude_rt()
    answer = await run(rt, 'how often does tram 28 run?')
    assert answer.outcome == 'answered'
    main, helper = fake.calls[0]['options'], fake.calls[1]['options']
    assert main.tools == [] and 'mcp__wraithd__web_search' in main.allowed_tools  # the main engine: no own tools
    assert helper.tools == ['WebSearch'] and helper.allowed_tools == ['WebSearch'] and helper.mcp_servers == {}
    events = rt.store.run_events(answer.run_id)
    [outcome] = [e.payload for e in events if e.type == ev.TOOL_RESULT]
    assert outcome['tool'] == 'web_search' and outcome['usage']['backend'] == 'claude'
    assert outcome['usage']['input_tokens'] == 900 and outcome['usage']['output_tokens'] == 150
    assert views.tokens_used(events) == 100 + 30 + 5 + 20 + 900 + 150  # the engine's and the search's
    handed = json.dumps(searching.answers)  # what Claude Code got back: the rows, wrapped as third-party text
    assert 'carris.pt/28' in handed and 'untrusted_tool_output' in handed
    assert outcome['untrusted'] is True
    [end] = [e.payload for e in events if e.type == ev.RUN_END]
    assert end['tokens'] == views.tokens_used(events)  # the run's budget counts the search's tokens


# --- audit -------------------------------------------------------------------------------------------------------

def test_audit_checks_the_helper_allows_exactly_websearch(claude_sdk, home, monkeypatch):
    from wraithd import audit
    from wraithd.config import load_config

    claude_sdk()
    set_values(home, 'web', {'search': 'claude'})
    [check] = audit.search_helper_rules(load_config(home))
    assert check.state == 'ok' and check.text == 'The Claude web search helper may use only WebSearch'
    assert check.text in [c.text for c in audit.checks(home)]
    loosened = claude_search.search_options

    def with_bash(model):
        options = loosened(model)
        options.tools = ['WebSearch', 'Bash']
        return options

    monkeypatch.setattr(claude_search, 'search_options', with_bash)
    [check] = audit.search_helper_rules(load_config(home))
    assert check.state == 'FAIL' and 'more than WebSearch' in check.text
