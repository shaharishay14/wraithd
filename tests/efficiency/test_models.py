"""E3: a model per purpose ([models]), each defaulting to today's model; doctor checks each configured model."""

import json

import httpx
import pytest

from local_agent import doctor, providers
from local_agent.config import Config, ConfigError, load_config, set_key
from local_agent.core import compaction
from local_agent.daemon import jobs
from local_agent.memory import distill
from local_agent.run import Runtime
from local_agent.store import SQLiteStore
from tests.scripted import ScriptedModel, run_once, say

TODAY = {'chat': 'default', 'strong': 'strong', 'distill': 'default', 'compaction': 'default',
         'heartbeat': 'default', 'subagent': 'default'}  # what each purpose used before [models]


def openai_config(home, models=''):
    set_key('openai', 'sk-test-' + 'x' * 30)
    home.config_path.write_text('[model]\nprovider = "openai"\ndefault = "gpt-4o-mini"\nstrong = "gpt-4o"\n'
                                + (f'\n[models]\n{models}' if models else ''))
    return load_config(home)


def names(models):
    return {key: model.model for key, model in models.items()}


def test_without_models_every_purpose_uses_the_model_it_always_did(home):
    models = providers.build_models(openai_config(home))
    assert names(models) == {'default': 'gpt-4o-mini', 'strong': 'gpt-4o'}
    for purpose, key in TODAY.items():
        assert providers.model_for(models, purpose) is models[key]


@pytest.mark.parametrize('purpose', providers.OWN_MODEL_PURPOSES)
def test_each_purpose_can_have_its_own_model(home, purpose):
    models = providers.build_models(openai_config(home, f'{purpose} = "gpt-4.1-nano"\n'))
    assert providers.model_for(models, purpose).model == 'gpt-4.1-nano'
    for other, key in TODAY.items():  # every other purpose is untouched
        if other != purpose:
            assert providers.model_for(models, other) is models[key]


def test_an_empty_name_is_unset_and_a_wrong_type_is_refused(home):
    assert 'distill' not in providers.build_models(openai_config(home, 'distill = ""\n'))
    with pytest.raises(ConfigError):
        openai_config(home, 'distill = 3\n')


def test_a_purpose_naming_the_default_model_shares_it(home):
    models = providers.build_models(openai_config(home, 'compaction = "gpt-4o-mini"\nsubagent = "gpt-4o"\n'))
    assert models['compaction'] is models['default'] and models['subagent'] is models['strong']


def runtime(home, **models):
    return Runtime(home=home, config=Config(), store=SQLiteStore(home.db_path), models=models)


async def test_compaction_and_distill_calls_use_their_own_models(home):
    compactor, distiller = ScriptedModel([[say('summary')]]), ScriptedModel([[say('{"facts": []}')]])
    rt = runtime(home, default=ScriptedModel([]), compaction=compactor, distill=distiller)
    try:
        assert await rt.completer('compaction')('fold these') == 'summary'
        assert await rt.complete('find facts', 'distill') == '{"facts": []}'
    finally:
        rt.close()
    assert compactor.inputs == [[{'role': 'user', 'content': 'fold these'}]]
    assert distiller.inputs == [[{'role': 'user', 'content': 'find facts'}]]


async def test_background_distill_uses_the_distill_model(home):
    default, distiller = ScriptedModel([]), ScriptedModel([[say('{"facts": []}')]])
    rt = runtime(home, default=default, distill=distiller)
    try:
        conversation = rt.store.start_conversation('terminal')
        for number in range(distill.MIN_NEW_MESSAGES):
            rt.store.add_message(conversation.id, 'user' if number % 2 == 0 else 'assistant', 'I prefer tea')
        await jobs.distill_all(rt)
    finally:
        rt.close()
    assert len(distiller.inputs) == 1 and default.inputs == []


async def test_history_compaction_uses_the_compaction_model(home):
    default, compactor = ScriptedModel([]), ScriptedModel([[say('They talked about tea.')]])
    rt = runtime(home, default=default, compaction=compactor)
    try:
        conversation = rt.store.start_conversation('terminal')
        for number in range(compaction.RECENT_MESSAGES_VERBATIM + compaction.COMPACT_BATCH_SIZE + 1):
            rt.store.add_message(conversation.id, 'user' if number % 2 == 0 else 'assistant', f'message {number}')
        await compaction.history_for(rt.store, conversation.id, rt.completer('compaction'))
    finally:
        rt.close()
    assert len(compactor.inputs) == 1 and default.inputs == []


@pytest.mark.parametrize('purpose', ['heartbeat', 'subagent'])
async def test_a_run_for_a_purpose_uses_its_model(home, purpose):
    default, own = ScriptedModel([]), ScriptedModel([[say('done')]])
    rt = runtime(home, default=default, **{purpose: own})
    try:
        result, _ = await run_once(rt, 'check things', purpose=purpose, owner=False, builtins=False)
    finally:
        rt.close()
    assert result.answer == 'done' and default.inputs == [] and len(own.inputs) == 1


async def test_an_unset_purpose_runs_on_the_default_model(home):
    default = ScriptedModel([[say('done')]])
    rt = runtime(home, default=default)
    try:
        result, _ = await run_once(rt, 'check things', purpose='heartbeat', owner=False, builtins=False)
    finally:
        rt.close()
    assert result.answer == 'done' and len(default.inputs) == 1


# --- doctor --------------------------------------------------------------------------------------------------

def listing(*ids, status=200):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status, json={'data': [{'id': name} for name in ids]})
    return httpx.MockTransport(handler), seen


def test_doctor_checks_every_configured_model(home):
    config = openai_config(home, 'distill = "gpt-4.1-nano"\nsubagent = "gpt-4o"\n')
    transport, seen = listing('gpt-4o-mini', 'gpt-4o')
    found = providers.doctor_checks(home, config, transport)
    assert [(check.state, check.text) for check in found] == [
        (doctor.OK, 'Model gpt-4o-mini (default): available'),
        (doctor.OK, 'Model gpt-4o (strong, subagent): available'),
        (doctor.FAIL, "Model gpt-4.1-nano (distill): not in openai's model list"),
    ]
    assert '[models]' in found[-1].fix
    [request] = seen  # one request, with the key, to the provider's list
    assert str(request.url) == 'https://api.openai.com/v1/models' and request.headers['authorization'].startswith(
        'Bearer sk-test-')


def test_doctor_never_fails_on_the_network_or_a_missing_key(home):
    config = openai_config(home)

    def refuse(request):
        raise httpx.ConnectError('offline')
    for transport in (httpx.MockTransport(refuse), listing(status=401)[0],
                      httpx.MockTransport(lambda r: httpx.Response(200, text='not json'))):
        [check] = providers.doctor_checks(home, config, transport)
        assert check.state == doctor.INFO and "couldn't get openai's model list" in check.text
    home.config_path.write_text('[model]\nprovider = "anthropic"\ndefault = "claude-sonnet-5-5"\n')
    transport, seen = listing('claude-sonnet-5-5')
    [check] = providers.doctor_checks(home, load_config(home), transport)  # no anthropic key: nothing is sent
    assert check.state == doctor.INFO and seen == []


def test_doctor_asks_ollama_its_own_endpoint(home):
    home.config_path.write_text('[model]\nprovider = "ollama"\ndefault = "qwen2.5:7b"\nstrong = ""\n')
    transport, seen = listing('qwen2.5:7b')
    [check] = providers.doctor_checks(home, load_config(home), transport)
    assert check.state == doctor.OK and str(seen[0].url) == 'http://localhost:11434/v1/models'


def test_the_doctor_command_includes_the_model_lines(home):
    openai_config(home)
    transport, _ = listing('gpt-4o-mini', 'gpt-4o')
    found = doctor.checks(home, service=None, transport=transport, port_free=lambda port: True)
    assert 'Model gpt-4o-mini (default): available' in [check.text for check in found]
    assert json.dumps([check.text for check in found])  # renders
