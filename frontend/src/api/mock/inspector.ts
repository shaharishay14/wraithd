// What the daemon derives from a run's events, ported from src/local_agent/core/inspector.py (summarize,
// disclosure_depth) and core/views.py, so mock runs carry the summary the real API will send.

import type { RunDetail, RunDisclosure, RunEvent, RunListItem, RunOutcome, RunSource, RunSummary } from '../types'

const first = (events: RunEvent[], type: string) => events.find((e) => e.type === type)
const ofType = (events: RunEvent[], type: string) => events.filter((e) => e.type === type)
const str = (value: unknown) => (typeof value === 'string' ? value : null)
const num = (value: unknown) => (typeof value === 'number' ? value : 0)

const toolsCalled = (events: RunEvent[]) => ofType(events, 'tool_call').map((e) => str(e.payload.tool) ?? '')

function untrustedContentSeen(events: RunEvent[]) {
  const sources = ofType(events, 'run_start').flatMap((e) => (e.payload.untrusted_inputs as string[] | undefined) ?? [])
  for (const event of ofType(events, 'tool_result')) {
    const tool = str(event.payload.tool) ?? ''
    if (event.payload.untrusted && !sources.includes(tool)) sources.push(tool)
  }
  return sources
}

function disclosureDepth(events: RunEvent[]): RunDisclosure {
  const called = toolsCalled(events)
  const payload = first(events, 'context_built')?.payload ?? {}
  const memory = called.includes('memory_read') ? 'L3 read' : called.includes('memory_search') ? 'L2 search'
    : payload.memory_notes != null ? 'L0-L1 prompt' : null
  const apps = called.includes('run_app_action') || called.some((t) => t && t === t.toUpperCase() && /[A-Z]/.test(t)) ? 'run'
    : called.includes('describe_app_action') ? 'describe' : called.includes('find_app_actions') ? 'find' : null
  return {
    mode: (str(payload.disclosure) as RunDisclosure['mode']) ?? null,
    apps,
    memory,
    skills_loaded: ofType(events, 'tool_call').filter((e) => e.payload.tool === 'skill_load')
      .map((e) => str((e.payload.arguments as Record<string, unknown> | undefined)?.name) ?? ''),
    web_pages_read: called.filter((t) => t === 'read_website').length,
  }
}

export function summarize(events: RunEvent[]): RunSummary {
  const start = first(events, 'run_start')
  const end = first(events, 'run_end')
  const context = first(events, 'context_built')
  const results = ofType(events, 'tool_result')
  const stops = ofType(events, 'stop')
  const source = str(start?.payload.source)
  return {
    source: (source === 'chat' || source === 'telegram' || source === 'schedule' ? source : null) as RunSource | null,
    message: str(start?.payload.message) ?? '',
    started_at: start?.at ?? null,
    outcome: ((end && str(end.payload.outcome)) ?? 'running') as RunOutcome,
    duration_ms: end ? num(end.payload.duration_ms) : null,
    turns: ofType(events, 'turn_start').length,
    tokens: ofType(events, 'model_output').reduce((sum, e) => sum + num(e.payload.input_tokens) + num(e.payload.output_tokens), 0),
    tool_calls: toolsCalled(events).length,
    tool_errors: results.filter((e) => e.payload.outcome === 'error').length,
    denied: results.filter((e) => e.payload.outcome === 'denied').length,
    stop_reason: stops.length ? str(stops[stops.length - 1].payload.reason) : null,
    untrusted_sources: untrustedContentSeen(events),
    reminders: ofType(events, 'reminder').map((e) => str(e.payload.name) ?? ''),
    redactions: [...new Set(results.flatMap((e) => (e.payload.redacted as string[] | undefined) ?? []))].sort(),
    tools_offered: ((context?.payload.tools_offered as unknown[] | undefined) ?? []).length,
    tool_schema_chars: context ? num(context.payload.tool_schema_chars) : null,
    instructions_chars: context ? num(context.payload.instructions_chars) : null,
    model: context ? str(context.payload.model) : null,
    model_upgrades: ofType(events, 'model_upgrade').map((e) => str(e.payload.model) ?? ''),
    disclosure: disclosureDepth(events),
  }
}

export const listItem = (run: RunDetail): RunListItem => {
  const { source, message, started_at, outcome, turns, tool_calls, tokens, stop_reason, untrusted_sources } = run.summary
  return { run_id: run.run_id, started_at, source, message, outcome, turns, tool_calls, tokens, stop_reason, untrusted_sources }
}
