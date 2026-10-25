import { ChevronRight, ShieldAlert } from 'lucide-react'

import type { RunListItem } from '../../api/types'
import { cn } from '../../lib/cn'
import { formatExact, formatNumber, formatWhen } from '../../lib/format'
import { href } from '../../lib/router'
import { Badge } from '../../ui/Badge'
import { panelClass } from '../../ui/Panel'
import { SOURCE_LABEL, STOP_LABEL, runBadge } from './utils'

// One run in the list: what was asked, how it ended, and what it cost
export function RunRow({ run }: { run: RunListItem }) {
  const badge = runBadge(run.outcome, run.stop_reason)
  return (
    <a
      href={href('runs', run.run_id)}
      className={cn(panelClass, 'group flex flex-col gap-3 px-4 py-3.5 no-underline transition-colors duration-150 hover:border-white/30 sm:flex-row sm:items-center sm:px-5')}
    >
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <div className="flex flex-wrap items-center gap-x-2 text-[13px] text-white/60">
          <span className="font-medium text-white/85">{run.source ? SOURCE_LABEL[run.source] : 'Run'}</span>
          <span aria-hidden="true">·</span>
          <span title={formatExact(run.started_at)}>{formatWhen(run.started_at)}</span>
          <span aria-hidden="true">·</span>
          <span className="font-mono text-xs text-white/45">{run.run_id.slice(0, 8)}</span>
        </div>
        <p className="m-0 truncate text-[15px] text-white/92">{run.message || <span className="text-white/50 italic">No message</span>}</p>
        {(run.stop_reason || run.untrusted_sources.length > 0) && (
          <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs">
            {run.stop_reason && <span className="text-busy">{STOP_LABEL[run.stop_reason] ?? run.stop_reason}</span>}
            {run.untrusted_sources.length > 0 && (
              <span className="inline-flex items-center gap-1 text-white/65">
                <ShieldAlert className="size-3.5" strokeWidth={1.7} aria-hidden="true" />
                Read third-party content: <span className="font-mono">{run.untrusted_sources.join(', ')}</span>
              </span>
            )}
          </div>
        )}
      </div>

      <dl className="m-0 flex shrink-0 items-center gap-5 text-sm">
        {([['Steps', run.turns], ['Tools', run.tool_calls], ['Tokens', run.tokens]] as const).map(([label, value]) => (
          <div key={label} className="flex flex-col items-end">
            <dt className="text-xs text-white/50">{label}</dt>
            <dd className="m-0 font-medium tabular-nums">{formatNumber(value)}</dd>
          </div>
        ))}
        <Badge status={badge.status} pulse={run.outcome === 'running'}>{badge.label}</Badge>
        <ChevronRight className="hidden size-4 text-white/45 transition-transform duration-200 group-hover:translate-x-0.5 sm:block" strokeWidth={1.7} aria-hidden="true" />
      </dl>
    </a>
  )
}
