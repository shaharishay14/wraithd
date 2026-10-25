import { useState } from 'react'
import { CalendarClock, Repeat } from 'lucide-react'

import { api, errorMessage } from '../../api/client'
import type { Schedule } from '../../api/types'
import { cn } from '../../lib/cn'
import { formatExact, formatWhen } from '../../lib/format'
import { useResource } from '../../lib/useResource'
import { Badge } from '../../ui/Badge'
import { Button } from '../../ui/Button'
import { PageBody, PageHeader } from '../../ui/PageHeader'
import { EmptyState, ErrorState, Loading, panelClass } from '../../ui/Panel'
import { Segmented } from '../../ui/Segmented'

function ScheduleRow({ schedule, onCancelled }: { schedule: Schedule; onCancelled: () => void }) {
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)
  const Icon = schedule.cron ? Repeat : CalendarClock

  const cancel = async () => {
    setBusy(true)
    setProblem(null)
    try {
      await api.cancelSchedule(schedule.id)
      onCancelled()
    } catch (e) {
      setProblem(errorMessage(e))
      setBusy(false)
    }
  }

  return (
    <li className={cn(panelClass, 'flex flex-col gap-3 p-4 sm:flex-row sm:items-center sm:px-5', !schedule.active && 'opacity-60')}>
      <Icon className="hidden size-5 shrink-0 text-white/50 sm:block" strokeWidth={1.7} aria-hidden="true" />
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <p className="m-0 text-[15px] break-words text-white">{schedule.message}</p>
        <p className="m-0 font-mono text-xs break-words text-white/55">{schedule.when}</p>
        <p className="m-0 flex flex-wrap gap-x-3 text-xs text-white/50">
          {schedule.next_fire_at && <span title={formatExact(schedule.next_fire_at)}>Next {formatWhen(schedule.next_fire_at)}</span>}
          <span>{schedule.last_fired_at ? <span title={formatExact(schedule.last_fired_at)}>Last ran {formatWhen(schedule.last_fired_at)}</span> : 'Never ran yet'}</span>
          <span>#{schedule.id}</span>
        </p>
        {problem && <p className="m-0 text-sm text-off" role="alert">{problem}</p>}
      </div>
      <div className="flex shrink-0 items-center gap-2">
        {!schedule.active ? (
          <Badge tone="quiet">Cancelled</Badge>
        ) : confirming ? (
          <>
            <Button variant="ghost" size="sm" onClick={() => setConfirming(false)} disabled={busy}>Keep</Button>
            <Button variant="danger" size="sm" onClick={cancel} loading={busy}>Cancel schedule</Button>
          </>
        ) : (
          <>
            <Badge status="live">{schedule.cron ? 'Repeats' : 'Once'}</Badge>
            <Button variant="glass" size="sm" onClick={() => setConfirming(true)} aria-label={`Cancel: ${schedule.message}`}>Cancel</Button>
          </>
        )}
      </div>
    </li>
  )
}

export default function Schedules() {
  const [all, setAll] = useState<'active' | 'all'>('active')
  const schedules = useResource(() => api.schedules(all === 'all'), [all])

  return (
    <>
      <PageHeader
        eyebrow="Schedules"
        title="What runs on its own"
        subtitle="Standing instructions you approved. They run unattended: no shell, and writes only if you pre-approved them. Your agent creates them when you ask; cancel them here."
        actions={<Segmented label="Show" value={all} onChange={setAll} items={[{ value: 'active', label: 'Active' }, { value: 'all', label: 'All' }]} />}
      />
      <PageBody>
        {schedules.loading && <Loading label="Loading schedules" />}
        {schedules.error && !schedules.data && <ErrorState message={schedules.error} onRetry={schedules.reload} />}
        {schedules.data && schedules.data.length === 0 && (
          <EmptyState title="No schedules.">Ask your agent: "every weekday at 8, send me my morning brief" or "remind me Friday to call Dana".</EmptyState>
        )}
        {schedules.data && schedules.data.length > 0 && (
          <ul className="m-0 flex list-none flex-col gap-2 p-0">
            {schedules.data.map((s) => <ScheduleRow key={s.id} schedule={s} onCancelled={schedules.reload} />)}
          </ul>
        )}
      </PageBody>
    </>
  )
}
