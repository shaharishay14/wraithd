// Ported from agent-studio frontend/src/components/pages/runs/utils, for the local event log (core/events.py).

import type { RunEvent, RunOutcome, RunSource } from '../../api/types'
import { formatDuration, formatNumber } from '../../lib/format'
import type { Status } from '../../ui/StatusDot'

export const OUTCOME_LABEL: Record<RunOutcome, string> = { answered: 'Answered', failed: 'Failed', refused: 'Refused', running: 'Running' }
const OUTCOME_STATUS: Record<RunOutcome, Status> = { answered: 'live', failed: 'off', refused: 'off', running: 'busy' }

export const SOURCE_LABEL: Record<RunSource, string> = { chat: 'Chat', telegram: 'Telegram', schedule: 'Schedule' }

// Stop conditions, in words (core/stop.py)
export const STOP_LABEL: Record<string, string> = {
  tool_errors: 'Stopped after repeated tool errors',
  repeated_call: 'Stopped a loop (same call again)',
  tool_calls: 'Stopped at the tool call limit',
  token_budget: 'Stopped at the run token budget',
  daily_budget: 'Daily usage limit reached',
  max_turns: 'Stopped at the step limit',
}

// A run the harness stopped did answer, but only after being cut off
export const runBadge = (outcome: RunOutcome, stopReason: string | null): { label: string; status: Status } =>
  outcome === 'answered' && stopReason ? { label: 'Stopped', status: 'busy' } : { label: OUTCOME_LABEL[outcome] ?? outcome, status: OUTCOME_STATUS[outcome] ?? 'idle' }

const str = (value: unknown) => (typeof value === 'string' ? value : value == null ? '' : JSON.stringify(value))
const num = (value: unknown) => (typeof value === 'number' ? value : 0)

export type EventTone = 'default' | 'good' | 'warn' | 'bad' | 'quiet'

// One line per event, in words: what the harness saw or decided at that step
export function describeEvent(event: RunEvent): { title: string; detail?: string; tone: EventTone } {
  const p = event.payload
  switch (event.type) {
    case 'run_start': {
      const where = p.source === 'schedule' ? 'Started by a schedule' : p.source === 'telegram' ? 'Started from Telegram' : 'Started from chat'
      const untrusted = (p.untrusted_inputs as string[] | undefined) ?? []
      return { title: where + (p.owner === false ? ' (not the owner)' : ''), detail: str(p.message) + (untrusted.length ? ` · third-party input: ${untrusted.join(', ')}` : ''), tone: 'quiet' }
    }
    case 'context_built': {
      const skills = (p.skills_offered as string[] | undefined) ?? []
      return {
        title: `Context built: ${(p.tools_offered as unknown[] | undefined)?.length ?? 0} tools offered (${str(p.disclosure) || 'direct'})${p.model ? ` · ${str(p.model)}` : ''}`,
        detail: `${formatNumber(num(p.tool_schema_chars))} chars of tool definitions · ${formatNumber(num(p.instructions_chars))} chars of instructions`
          + (p.memory_notes != null ? ` · ${num(p.memory_notes)} memory notes` : '') + (skills.length ? ` · skills: ${skills.join(', ')}` : ''),
        tone: 'quiet',
      }
    }
    case 'turn_start':
      return { title: `Model call ${num(p.turn)}`, detail: p.input_items != null ? `${num(p.input_items)} input items` : undefined, tone: 'quiet' }
    case 'model_output': {
      const calls = (p.tool_calls as string[] | undefined) ?? []
      return {
        title: calls.length ? `Model asked for ${calls.join(', ')}` : `Model answered (${formatNumber(num(p.text_chars))} chars)`,
        detail: `${formatNumber(num(p.input_tokens))} tokens in · ${formatNumber(num(p.output_tokens))} out`,
        tone: 'default',
      }
    }
    case 'tool_call': {
      const args = p.arguments && typeof p.arguments === 'object' ? JSON.stringify(p.arguments) : ''
      const shown = args && args !== '{}' ? (args.length > 120 ? args.slice(0, 117) + '...' : args) : 'no arguments'
      return p.allowed
        ? { title: `Called ${str(p.tool)}`, detail: `${str(p.access)} · ${shown}`, tone: 'default' }
        : { title: `Blocked ${str(p.tool)}`, detail: p.rejected_by ? `rejected by the ${str(p.rejected_by)} · ${shown}` : `${str(p.access)} · ${shown}`, tone: 'warn' }
    }
    case 'tool_result': {
      const outcome = str(p.outcome)
      const redacted = (p.redacted as string[] | undefined) ?? []
      const flags = [p.untrusted && 'third-party content', p.truncated && 'truncated', redacted.length && `secrets removed: ${redacted.join(', ')}`].filter(Boolean).join(' · ')
      return {
        title: `${str(p.tool)} → ${outcome}`,
        detail: [flags, `${formatNumber(num(p.chars))} chars`].filter(Boolean).join(' · '),
        tone: outcome === 'ok' ? (p.untrusted ? 'warn' : 'good') : outcome === 'denied' ? 'warn' : 'bad',
      }
    }
    case 'reminder':
      return { title: `Reminder: ${str(p.name).replace(/_/g, ' ')}`, detail: `added after ${str(p.at).replace('*', 'a tool result')}`, tone: 'quiet' }
    case 'stop':
      return { title: STOP_LABEL[str(p.reason)] ?? `Stopped: ${str(p.reason)}`, detail: str(p.message), tone: 'warn' }
    case 'model_upgrade':
      return { title: `Switched to the strong model: ${str(p.model)}`, detail: p.skill ? `asked by the ${str(p.skill)} skill` : undefined, tone: 'default' }
    case 'run_end':
      return {
        title: `Finished: ${str(p.outcome)}`,
        detail: `${formatDuration(num(p.duration_ms))} · ${formatNumber(num(p.tokens))} tokens` + (p.stop_reason ? ` · ${STOP_LABEL[str(p.stop_reason)] ?? str(p.stop_reason)}` : ''),
        tone: p.outcome === 'answered' ? (p.stop_reason ? 'warn' : 'good') : 'bad',
      }
    default:
      return { title: event.type.replace(/_/g, ' '), tone: 'quiet' }
  }
}
