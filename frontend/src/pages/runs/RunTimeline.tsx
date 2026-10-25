import { useState } from 'react'
import { ChevronDown } from 'lucide-react'

import type { RunEvent } from '../../api/types'
import { cn } from '../../lib/cn'
import { formatExact } from '../../lib/format'
import { Button } from '../../ui/Button'
import { panelClass } from '../../ui/Panel'
import { describeEvent, type EventTone } from './utils'

const DOT: Record<EventTone, string> = {
  default: 'bg-white',
  good: 'bg-live',
  warn: 'bg-busy',
  bad: 'bg-off',
  quiet: 'bg-white/35',
}

function offset(event: RunEvent, start: string | null) {
  if (!event.at || !start) return null
  const ms = new Date(event.at).getTime() - new Date(start).getTime()
  return ms < 1000 ? `+${ms} ms` : `+${(ms / 1000).toFixed(1)} s`
}

function TimelineItem({ event, start, open, onToggle }: { event: RunEvent; start: string | null; open: boolean; onToggle: () => void }) {
  const { title, detail, tone } = describeEvent(event)
  const id = `event-${event.seq}`
  const when = offset(event, start)
  return (
    <li className="relative ps-6">
      <span className={cn('absolute top-[11px] -left-[5px] size-2.5 rounded-full ring-4 ring-night-2', DOT[tone])} aria-hidden="true" />
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        aria-controls={id}
        className="flex w-full items-start gap-3 rounded-xl px-2 py-1.5 text-left hover:bg-white/4"
      >
        <span className="flex min-w-0 flex-1 flex-col">
          <span className={cn('text-[14px] leading-[22px] break-words', tone === 'quiet' ? 'text-white/66' : 'text-white')}>{title}</span>
          {detail && <span className="text-[13px] leading-5 break-words text-white/55">{detail}</span>}
        </span>
        <span className="flex shrink-0 flex-col items-end pt-0.5 text-xs text-white/40 tabular-nums">
          <span>#{event.seq}</span>
          {when && <span title={formatExact(event.at)} className="hidden sm:inline">{when}</span>}
        </span>
        <ChevronDown className={cn('mt-1 size-4 shrink-0 text-white/45 transition-transform duration-200', open && 'rotate-180')} strokeWidth={1.7} aria-hidden="true" />
      </button>
      {open && (
        <div id={id} className="mx-2 mt-1 mb-2 rounded-xl border border-white/8 bg-night/70">
          <div className="flex justify-between border-b border-white/8 px-3 py-1.5 font-mono text-[11px] text-white/50">
            <span>{event.type}</span>
            {event.at && <span>{event.at}</span>}
          </div>
          <pre className="m-0 max-h-80 overflow-auto p-3 font-mono text-xs leading-5 break-words whitespace-pre-wrap text-white/85">{JSON.stringify(event.payload, null, 2)}</pre>
        </div>
      )}
    </li>
  )
}

// Every step of the run, in order; open one to see exactly what was logged
export function RunTimeline({ events }: { events: RunEvent[] }) {
  const [open, setOpen] = useState<Set<number>>(() => new Set())
  const start = events[0]?.at ?? null
  const allOpen = events.length > 0 && open.size === events.length
  const toggle = (seq: number) =>
    setOpen((current) => {
      const next = new Set(current)
      if (next.has(seq)) next.delete(seq)
      else next.add(seq)
      return next
    })

  return (
    <section className={cn(panelClass, 'p-4 sm:p-5')} aria-labelledby="timeline-heading">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h2 id="timeline-heading" className="m-0 font-display text-[17px] leading-6 font-medium tracking-[-0.015em]">
          Step by step <span className="ms-1 text-sm font-normal text-white/50">{events.length} events</span>
        </h2>
        <Button variant="ghost" size="sm" onClick={() => setOpen(allOpen ? new Set() : new Set(events.map((e) => e.seq)))}>
          {allOpen ? 'Collapse all' : 'Expand all'}
        </Button>
      </div>
      <ol className="m-0 ms-1.5 flex list-none flex-col gap-0.5 border-s border-white/10 p-0">
        {events.map((event) => (
          <TimelineItem key={event.seq} event={event} start={start} open={open.has(event.seq)} onToggle={() => toggle(event.seq)} />
        ))}
      </ol>
    </section>
  )
}
