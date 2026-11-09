"""
The system prompt says what the agent runs on and what it can do, in few tokens: skills by name with one line each,
skills that are off named with what turns them on, nothing per message, never a key. The sizes here are the budget:
a change that grows the prompt much fails, and says by how much.
"""

import pytest

from tests.scripted import ScriptedModel, fake_tool, run_once, say
from wraithd import run as run_module
from wraithd.config import Home, set_key
from wraithd.core import tokens
from wraithd.run import Runtime, setup_line

# A typical owner's own skills: one always on, three that need something this home doesn't have
OWNER_SKILLS = {
    'news-feed': ('requires: [web_search]\nmodel: strong',
                  'A morning digest of the news on the topics the user follows, with links.'),
    'x-digest': ('requires: [read_website]', 'Reads the accounts the user follows on X and sums up what matters.'),
    'gym-log': ('requires: [memory]', 'Logs a workout the user describes and compares it with the last ones.'),
    'expenses': ('requires: [email]', 'Finds receipts in the inbox and adds them to the monthly expenses note.'),
}
BODY = 'Step 1. Read the sources.\nStep 2. Write the digest.'
# Tokens (core/tokens.py) of the whole system prompt. 0.1.0 had 414 (empty) and 451 (typical); 0.1.1 adds the
# setup line, the next-step rule and the off-skills line, then the no-recap rule (NO_REPLY after notify_owner,
# 21 tokens). 0.1.2 (538 / 580 measured) adds the schedule-only rule (43) and the one-action rule (38): 619 / 661
BUDGET = {'empty': 551 + 43 + 38, 'typical': 596 + 43 + 38}


def write_owner_skills(home: Home) -> None:
    for name, (meta, description) in OWNER_SKILLS.items():
        folder = home.skills_dir / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: {description}\n{meta}\n---\n{BODY}\n')
        (folder / 'sources.md').write_text('- https://example.com/feed\n')


async def system_prompt(make_runtime, message='hi', **options) -> tuple[str, Runtime]:
    model = ScriptedModel([[say('ok')]])
    rt = make_runtime(model)
    await run_once(rt, message, **options)
    return model.instructions[0], rt


@pytest.mark.parametrize('setup', ['empty', 'typical'])
async def test_the_prompt_stays_within_its_budget(setup, home, make_runtime):
    if setup == 'typical':
        write_owner_skills(home)
    prompt, _ = await system_prompt(make_runtime)
    used = tokens.count(prompt)
    assert used <= BUDGET[setup], f'the {setup} prompt is {used} tokens, over its budget of {BUDGET[setup]}'


async def test_skills_are_names_and_one_line_bodies_and_files_wait_for_skill_load(home, make_runtime):
    write_owner_skills(home)
    prompt, _ = await system_prompt(make_runtime)
    assert '- gym-log: Logs a workout' in prompt and '- x-digest: Reads the accounts' in prompt
    assert 'Step 1' not in prompt and 'example.com' not in prompt


async def test_skills_that_are_off_say_what_turns_them_on(home, make_runtime):
    write_owner_skills(home)
    prompt, _ = await system_prompt(make_runtime)
    off = next(line for line in prompt.splitlines() if line.startswith('Skills that are off'))
    assert 'expenses, inbox-triage, morning-brief need an email app (wraithd mcp add)' in off
    assert 'meeting-prep needs a calendar app' in off
    assert ('news-feed, research-report need web search ([web] search: a key, or "claude" on the Claude '
            'subscription)') in off
    assert '- news-feed' not in prompt  # not offered as a skill it can load


async def test_a_skill_turns_on_when_its_requirement_is_met(home, make_runtime):
    write_owner_skills(home)
    prompt, _ = await system_prompt(make_runtime, tools=[fake_tool('web_search', lambda **a: {'results': []})])
    assert '- news-feed: A morning digest' in prompt and 'news-feed' not in prompt.split('Skills that are off')[1]


async def test_only_the_owner_hears_which_skills_are_off(home, make_runtime):
    write_owner_skills(home)
    prompt, _ = await system_prompt(make_runtime, owner=False)
    assert 'Skills that are off' not in prompt and 'expenses' not in prompt and 'gym-log' not in prompt


async def test_a_run_limited_to_some_tools_does_not_list_skills_as_off(home, make_runtime):
    write_owner_skills(home)
    prompt, _ = await system_prompt(make_runtime, allowed_tools=['read_website'])
    assert 'Skills that are off' not in prompt


async def test_the_prompt_asks_for_one_next_step_not_a_generic_offer(make_runtime):
    prompt, _ = await system_prompt(make_runtime)
    assert 'When there is one right next step or question, end with it; never a generic "anything else?"' in prompt


async def test_a_one_write_task_is_answered_in_the_same_turn(make_runtime):
    """"skip D1" made its write in turn 1, then spent turn 2 only to say "Done"."""
    prompt, _ = await system_prompt(make_runtime)
    assert ('When one write is the whole task (skip, save, add, remove), make it and give your one-line answer in '
            "the same turn; don't spend a turn just to say done.") in prompt


async def test_nothing_in_the_prompt_changes_from_message_to_message(home, make_runtime):
    write_owner_skills(home)
    first, _ = await system_prompt(make_runtime, 'what is on today?')
    second, _ = await system_prompt(make_runtime, 'can you read my news-feed?')
    assert first == second


# --- the setup line ------------------------------------------------------------------------------------------

def runtime_with(home: Home, config: str, monkeypatch, models=None) -> Runtime:
    home.config_path.write_text(config)
    monkeypatch.setattr(run_module, 'engine_models', lambda config: {})
    return Runtime.open(home, models=models)


def test_an_api_engine_says_which_provider_and_that_it_is_paid_per_use(home, monkeypatch):
    set_key('openai', 'sk-test-0123456789abcdef0123456789abcdef')
    rt = runtime_with(home, '[model]\nprovider = "openai"\ndefault = "gpt-4o-mini"\nstrong = "gpt-4o"\n', monkeypatch,
                      models={'default': ScriptedModel([])})
    line = setup_line(rt)
    assert line == ('You run on the openai API on the user\'s own key, paid per use, no subscription: gpt-4o-mini '
                    '(gpt-4o for harder skills). Telegram: off.\n')
    assert 'sk-test' not in line
    rt.close()


def test_claude_code_says_it_is_the_subscription(home, monkeypatch):
    rt = runtime_with(home, '[engine]\nname = "claude-code"\nchat_model = "opus"\n', monkeypatch)
    assert setup_line(rt) == ('You run on Claude Code on the user\'s Claude subscription (no API key): opus for '
                              'chats, haiku for background work. Telegram: off.\n')
    rt.close()


def test_claude_code_with_a_key_says_where_pictures_go(home, monkeypatch):
    rt = runtime_with(home, '[model]\nprovider = "anthropic"\ndefault = "claude-haiku-5-5"\n'
                            '[engine]\nname = "claude-code"\n', monkeypatch, models={'default': ScriptedModel([])})
    assert '; pictures: anthropic API, claude-haiku-5-5.' in setup_line(rt)
    rt.close()


def test_a_local_model_says_so(home, monkeypatch):
    rt = runtime_with(home, '[model]\nprovider = "ollama"\ndefault = "llama3.2"\nstrong = ""\n'
                            'base_url = "http://localhost:11434/v1"\n', monkeypatch,
                      models={'default': ScriptedModel([])})
    assert setup_line(rt).startswith('You run on a local model (Ollama, llama3.2), no subscription or API key.')
    assert 'localhost' not in setup_line(rt)
    rt.close()


def test_a_paired_telegram_is_named(home, monkeypatch):
    rt = runtime_with(home, '', monkeypatch, models={'default': ScriptedModel([])})
    rt.store.set_setting(run_module.TELEGRAM_PAIRED, '42')
    assert setup_line(rt).endswith('Telegram: paired.\n')
    rt.close()


def test_the_telegram_setting_name_matches_the_channel():
    from wraithd.channels.telegram import OWNER_CHAT
    assert run_module.TELEGRAM_PAIRED == OWNER_CHAT
