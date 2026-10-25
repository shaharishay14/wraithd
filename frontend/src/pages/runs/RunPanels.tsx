import type { RunSummary } from '../../api/types'
import { formatDuration, formatNumber } from '../../lib/format'
import { Code, Panel, Rows, None } from '../../ui/Panel'
import { STOP_LABEL } from './utils'

// The run in numbers
export function RunStats({ summary }: { summary: RunSummary }) {
  const stats: [string, string][] = [
    ['Model calls', formatNumber(summary.turns)],
    ['Tool calls', formatNumber(summary.tool_calls)],
    ['Tokens', formatNumber(summary.tokens)],
    ['Duration', formatDuration(summary.duration_ms)],
    ['Tool definitions', summary.tool_schema_chars == null ? '—' : `${formatNumber(summary.tool_schema_chars)} chars`],
  ]
  return (
    <dl className="m-0 grid grid-cols-2 gap-2.5 sm:grid-cols-3 lg:grid-cols-5">
      {stats.map(([label, value]) => (
        <div key={label} className="rounded-tile border border-white/10 bg-night-2 px-4 py-3">
          <dt className="text-xs text-white/60">{label}</dt>
          <dd className="m-0 font-display text-[21px] leading-8 font-medium tabular-nums">{value}</dd>
        </div>
      ))}
    </dl>
  )
}

const list = (items: string[]) => (items.length ? <span className="font-mono text-[13px]">{items.join(', ')}</span> : <None />)

// What the model was given, and what it pulled in on demand
export function DisclosurePanel({ summary }: { summary: RunSummary }) {
  const { disclosure } = summary
  return (
    <Panel title="What it pulled in">
      <Rows
        rows={[
          ['Model', summary.model ? <Code>{summary.model}</Code> : <None />],
          ['Strong model', summary.model_upgrades.length ? list(summary.model_upgrades) : <None />],
          ['App actions', disclosure.mode === 'discovery' ? (disclosure.apps ? `discovered (${disclosure.apps})` : 'discovery, not used') : `${summary.tools_offered} tools given directly`],
          ['Memory', disclosure.memory ?? 'Off'],
          ['Skills loaded', list(disclosure.skills_loaded)],
          ['Web pages read', disclosure.web_pages_read || <None />],
          ['Instructions', summary.instructions_chars == null ? '—' : `${formatNumber(summary.instructions_chars)} chars`],
        ]}
      />
    </Panel>
  )
}

// What the harness guarded against in this run
export function SafetyPanel({ summary }: { summary: RunSummary }) {
  return (
    <Panel title="Safety">
      <Rows
        rows={[
          ['Third-party content', summary.untrusted_sources.length ? <span className="font-mono text-[13px] text-busy">{summary.untrusted_sources.join(', ')}</span> : <None />],
          ['Blocked or declined', summary.denied || <None />],
          ['Tool errors', summary.tool_errors || <None />],
          ['Secrets removed', list(summary.redactions)],
          ['Stop', summary.stop_reason ? <span className="text-busy">{STOP_LABEL[summary.stop_reason] ?? summary.stop_reason}</span> : <span className="text-white/45">Ended on its own</span>],
          ['Reminders', summary.reminders.length ? summary.reminders.map((name) => name.replace(/_/g, ' ')).join(', ') : <None />],
        ]}
      />
    </Panel>
  )
}
