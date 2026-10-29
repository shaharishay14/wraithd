"""
delegate: the agent hands one self-contained task to a subagent, a child run with a context of its own, and gets a
summary back instead of everything the child read. The parent's context stays small; the rules don't loosen:

- Budget: the child gets at most what's left of the parent's run budget (and of the daily one). Its tokens count
  toward the parent's run budget (a subagent_result event, views.delegated_tokens) and, like every run's, toward
  the daily budget. The parent's own run_end counts only its own tokens, so the daily sum isn't doubled.
- Taint flows both ways: the child starts with everything third-party the parent had read (inherited_taint), and
  a child that read third-party content returns an untrusted result, which taints the parent. When the parent is
  tainted, the task itself is the model's words after reading someone else's, so the child's gate doesn't count it
  as the owner's (addresses in it aren't the owner's recipients).
- Depth 1: a child is never offered delegate.
- Nobody watching the parent means nobody watching the child: an unattended parent makes an unattended child. In a
  conversation the child's approval cards reach the owner, but its text doesn't stream into the chat.
- The child uses the subagent model (model_for('subagent')), and its run_start names the parent (parent_run_id).
"""

import logging

from local_agent.core import events as ev
from local_agent.core import views
from local_agent.tools.base import clamp_int, function_tool, tool_error

logger = logging.getLogger(__name__)

DELEGATE = 'delegate'
SOURCE = 'subagent'
DELEGATED_TASK = 'delegated task'  # the taint source for a task written after the parent read third-party text
MIN_TOKENS = 2_000
SUMMARY_CHARS = 4_000
# What of the child's callback reaches the parent's channel: approvals only, never streamed text
FORWARDED = {'approval_required', 'approval_resolved', 'heartbeat'}

CHILD_BRIEF = ('You are a subagent working on one task for the main agent. Do the task with your tools, then answer '
               'with a short summary of what you found or did: facts, sources and open questions. Your answer is all '
               'the main agent will see.\n\nTask:\n')


def forwarding(callback):
    """The child's callback: the parent's, for approval cards only. None stays None (unattended)."""
    if callback is None:
        return None

    async def forward(event_type, data):
        if event_type in FORWARDED:
            await callback(event_type, data)

    return forward


def remaining(limit, used: int) -> int | None:
    return None if limit is None else max(limit - used, 0)


def delegate_tools(rt, gate, log, *, callback, owner: bool, mcp, parent_run_id, tools, app_tools, own,
                   allowed_tools) -> list:
    """[delegate] for the owner's top-level runs when [subagents] is on; [] for a child (depth 1)."""
    settings = rt.config.section('subagents')
    if parent_run_id is not None or not owner or not settings['enabled']:
        return []
    default_tokens = settings['default_tokens']
    from local_agent.run import run  # run.py builds this tool, so it's imported only once run.py has loaded

    # The child gets the parent's tools as they are (built-ins already chosen for attended or not, plus a caller's),
    # its own memory and skill tools on its own gate, and the same app connections. Never delegate.
    tools, app_tools = list(tools), [tool for tool in app_tools if mcp is None or tool not in mcp.tools]

    def child_tools() -> set[str]:
        names = {tool.name for tool in [*tools, *app_tools, *own, *(mcp.tools if mcp else [])]} - {DELEGATE}
        return names if allowed_tools is None else names & set(allowed_tools)

    async def delegate(task: str, budget_tokens=None) -> dict:
        task = (task or '').strip()
        if not task:
            return tool_error('The task is empty.', 'Write the task as a self-contained request.')
        used = views.tokens_used(log.events) + views.delegated_tokens(log.events)
        run_left = remaining(rt.config.run_tokens, used)
        daily_left = remaining(rt.config.daily_tokens, used)
        left = min(x for x in (run_left, daily_left, 10**12) if x is not None)
        if left < MIN_TOKENS:
            return tool_error('There is not enough of the token budget left for a subagent.',
                              'Finish the task yourself with what you have.')
        tokens = clamp_int(budget_tokens, MIN_TOKENS, left, min(default_tokens, left))
        budget = {'run_tokens': tokens}
        if daily_left is not None:
            budget['daily_tokens'] = rt.config.daily_tokens - used  # today's saved runs + this one so far
        tainted = bool(gate.untrusted_sources)
        result = await run(rt, CHILD_BRIEF + task, callback=forwarding(callback), source=SOURCE, owner=owner,
                           tools=tools, app_tools=app_tools, builtins=False, mcp=mcp, untrusted_source=DELEGATED_TASK if tainted else '', parent_run_id=log.run_id,
                           inherited_taint=list(gate.untrusted_texts), budget=budget, purpose='subagent',
                           allowed_tools=child_tools())
        child = rt.store.run_events(result.run_id)
        child_tokens = views.tokens_used(child)
        sources = [source for source in views.untrusted_content_seen(child) if source != DELEGATED_TASK]
        log.append(ev.SUBAGENT_RESULT, child_run_id=result.run_id, outcome=result.outcome, tokens=child_tokens,
                   budget_tokens=tokens, untrusted_sources=sources)
        summary = result.answer if len(result.answer) <= SUMMARY_CHARS else result.answer[:SUMMARY_CHARS] + ' [...]'
        found = {'summary': summary, 'child_run_id': result.run_id, 'outcome': result.outcome,
                 'tokens': child_tokens}
        if sources or tainted:
            # The summary was written after reading third-party content: the parent treats it as such
            found.update(untrusted=True, third_party_sources=sources or list(gate.untrusted_sources))
        return found

    return [function_tool(
        DELEGATE,
        ('Hand one self-contained task (research, reading several pages or documents) to a subagent with its own '
         'context. It returns a short summary, not everything it read. Use it when the details would crowd your '
         'context; its tokens count toward your budget.'),
        {'properties': {
            'task': {'type': 'string', 'description': 'The task, with everything the subagent needs to know'},
            'budget_tokens': {'type': 'integer',
                              'description': f'Optional token limit for the subagent (default {default_tokens})'}},
         'required': ['task']},
        delegate)]
