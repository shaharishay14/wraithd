import { useState } from 'react'
import { ChevronDown } from 'lucide-react'

import { api } from '../../api/client'
import type { Approval, ApprovalStatus } from '../../api/types'
import { cn } from '../../lib/cn'
import { argumentText, formatExact, formatWhen } from '../../lib/format'
import { usePendingApprovals } from '../../lib/pending'
import { useResource } from '../../lib/useResource'
import { ApprovalCard } from '../../ui/ApprovalCard'
import { APPROVAL_STATUS } from '../../ui/approvalStatus'
import { Badge } from '../../ui/Badge'
import { PageBody, PageHeader } from '../../ui/PageHeader'
import { EmptyState, ErrorState, Loading, panelClass } from '../../ui/Panel'
import { Segmented } from '../../ui/Segmented'

const DECIDED: ApprovalStatus[] = ['approved', 'denied', 'expired', 'preapproved']
type Filter = 'all' | ApprovalStatus

// The contract lists one status per call, so the history is the four decided statuses merged
const loadHistory = async () =>
  (await Promise.all(DECIDED.map((status) => api.approvals(status)))).flat().sort((a, b) => b.created_at.localeCompare(a.created_at))

function HistoryRow({ approval }: { approval: Approval }) {
  const [open, setOpen] = useState(false)
  const state = APPROVAL_STATUS[approval.status]
  const firstArg = Object.entries(approval.arguments)[0]
  const id = `approval-${approval.approval_id}`
  return (
    <li className={cn(panelClass, 'px-4 py-3')}>
      <button type="button" onClick={() => setOpen((v) => !v)} aria-expanded={open} aria-controls={id} className="flex w-full items-center gap-3 rounded-xl text-left">
        <span className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="font-mono text-sm break-words">{approval.tool}</span>
            {(approval.suspicious_recipients.length > 0 || approval.untrusted_sources.length > 0) && <Badge tone="warn">Warned</Badge>}
          </span>
          {firstArg && (
            <span className="truncate text-[13px] text-white/55">
              {firstArg[0]}: {argumentText(firstArg[1]).replace(/\s+/g, ' ')}
            </span>
          )}
        </span>
        <span className="hidden text-xs text-white/50 sm:inline" title={formatExact(approval.created_at)}>{formatWhen(approval.created_at)}</span>
        <Badge status={state.status}>{approval.status === 'preapproved' ? 'Always allowed' : state.label.split(',')[0]}</Badge>
        <ChevronDown className={cn('size-4 shrink-0 text-white/45 transition-transform duration-200', open && 'rotate-180')} strokeWidth={1.7} aria-hidden="true" />
      </button>
      {open && (
        <div id={id} className="mt-3">
          <ApprovalCard approval={approval} />
        </div>
      )}
    </li>
  )
}

export default function Approvals() {
  const pending = usePendingApprovals()
  const history = useResource(loadHistory, [], 5000)
  const [filter, setFilter] = useState<Filter>('all')
  const shown = (history.data ?? []).filter((a) => filter === 'all' || a.status === filter)
  const count = (status: ApprovalStatus) => history.data?.filter((a) => a.status === status).length

  return (
    <>
      <PageHeader
        eyebrow="Approvals"
        title={pending.length ? `${pending.length} waiting for you` : 'Nothing waiting'}
        subtitle="Anything that writes, sends, schedules or runs a command stops here until you decide. Unanswered cards expire after 60 seconds and don't run."
      />
      <PageBody>
        <section aria-labelledby="pending-heading" className="mb-10">
          <h2 id="pending-heading" className="m-0 mb-3 font-display text-lg font-medium tracking-[-0.015em]">Waiting</h2>
          {pending.length === 0 ? (
            <EmptyState title="No approvals waiting.">When the agent wants to do something that needs you, the card shows up here and in the chat.</EmptyState>
          ) : (
            <ul className="m-0 grid list-none gap-4 p-0 lg:grid-cols-2">
              {pending.map((approval) => (
                <li key={approval.approval_id}>
                  <ApprovalCard approval={approval} onChange={() => void history.reload()} />
                </li>
              ))}
            </ul>
          )}
        </section>

        <section aria-labelledby="history-heading">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
            <h2 id="history-heading" className="m-0 font-display text-lg font-medium tracking-[-0.015em]">History</h2>
            <Segmented
              label="Filter history"
              value={filter}
              onChange={setFilter}
              items={[
                { value: 'all', label: 'All', count: history.data?.length },
                { value: 'approved', label: 'Approved', count: count('approved') },
                { value: 'denied', label: 'Denied', count: count('denied') },
                { value: 'expired', label: 'Expired', count: count('expired') },
                { value: 'preapproved', label: 'Always allowed', count: count('preapproved') },
              ]}
            />
          </div>
          {history.loading && <Loading label="Loading approvals" />}
          {history.error && !history.data && <ErrorState message={history.error} onRetry={history.reload} />}
          {history.data && shown.length === 0 && <EmptyState title="Nothing here yet." />}
          {shown.length > 0 && (
            <ul className="m-0 flex list-none flex-col gap-2 p-0">
              {shown.map((approval) => <HistoryRow key={approval.approval_id} approval={approval} />)}
            </ul>
          )}
        </section>
      </PageBody>
    </>
  )
}
