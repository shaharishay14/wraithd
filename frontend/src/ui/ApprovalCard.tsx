import { useId, useState } from 'react'
import { ArrowUpRight, Check, Info, TriangleAlert, X } from 'lucide-react'

import { ApiError, api } from '../api/client'
import type { Approval } from '../api/types'
import { cn } from '../lib/cn'
import { argumentText, formatExact, formatWhen } from '../lib/format'
import { refreshPending } from '../lib/pending'
import { href } from '../lib/router'
import { useNow } from '../lib/useResource'
import { Badge } from './Badge'
import { Button } from './Button'
import { APPROVAL_STATUS } from './approvalStatus'
import { StatusDot } from './StatusDot'

const CHANNEL: Record<string, string> = { web: 'Web', terminal: 'Terminal', telegram: 'Telegram', schedule: 'Schedule' }

function Countdown({ expiresAt }: { expiresAt: string }) {
  const now = useNow(1000)
  const seconds = Math.max(0, Math.round((new Date(expiresAt).getTime() - now) / 1000))
  return (
    <Badge tone={seconds <= 15 ? 'warn' : 'quiet'} title={`Expires ${formatExact(expiresAt)}`}>
      <span className="tabular-nums">{seconds > 0 ? `Expires in ${seconds} s` : 'Expiring'}</span>
    </Badge>
  )
}

// What the agent wants to do, exactly as it asked, with every warning the gate attached (docs/api.md, Approvals).
// Mirrors the terminal's card (channels/terminal.py): tool, each argument, reason, then the warnings.
export function ApprovalCard({ approval, onChange, compact = false }: {
  approval: Approval
  onChange?: (approval: Approval) => void
  compact?: boolean
}) {
  const titleId = useId()
  const [deciding, setDeciding] = useState<'approve' | 'deny' | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const pending = approval.status === 'pending'
  const state = APPROVAL_STATUS[approval.status]
  const warned = approval.suspicious_recipients.length > 0 || approval.untrusted_sources.length > 0
  const args = Object.entries(approval.arguments)

  const decide = async (decision: 'approve' | 'deny') => {
    setDeciding(decision)
    setProblem(null)
    try {
      onChange?.(await api.decide(approval.approval_id, decision))
    } catch (e) {
      // 409: decided elsewhere or expired meanwhile. The message says its current status.
      setProblem(e instanceof ApiError ? e.message : 'Could not send your decision.')
    } finally {
      setDeciding(null)
      void refreshPending()
    }
  }

  return (
    <article
      aria-labelledby={titleId}
      className={cn(
        'flex w-full flex-col gap-4 rounded-card border bg-night-2 p-4 sm:p-5',
        pending ? (warned ? 'border-busy/55' : 'border-white/30') : 'border-white/12',
        pending && 'shadow-[0_0_0_4px_rgba(255,196,107,0.06),0_20px_50px_-24px_rgba(0,0,0,0.7)]',
      )}
    >
      <header className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
        <span className="flex items-center gap-2 text-sm font-semibold" aria-live="polite">
          <StatusDot status={state.status} pulse={pending} />
          {state.label}
        </span>
        <span className="flex flex-wrap items-center gap-1.5">
          {!compact && <Badge tone="quiet">{CHANNEL[approval.channel] ?? approval.channel}</Badge>}
          {pending && approval.expires_at ? <Countdown expiresAt={approval.expires_at} /> : (
            <Badge tone="quiet" title={formatExact(approval.created_at)}>{formatWhen(approval.created_at)}</Badge>
          )}
        </span>
      </header>

      <div className="flex flex-col gap-0.5">
        <span className="text-[13px] text-white/60">The agent wants to run</span>
        <h3 id={titleId} className="m-0 font-mono text-[16px] leading-6 font-medium break-words">{approval.tool}</h3>
      </div>

      {args.length > 0 ? (
        <dl className="m-0 flex flex-col gap-2.5">
          {args.map(([key, value]) => (
            <div key={key} className="flex flex-col gap-1">
              <dt className="font-mono text-xs text-white/60">{key}</dt>
              <dd className="m-0 max-h-56 overflow-auto rounded-xl border border-white/8 bg-night/70 px-3 py-2 font-mono text-[13px] leading-5 break-words whitespace-pre-wrap text-white/92">
                {argumentText(value)}
              </dd>
            </div>
          ))}
        </dl>
      ) : (
        <p className="m-0 text-sm text-white/55">No arguments.</p>
      )}

      {approval.reason && (
        <p className="m-0 flex gap-2.5 text-sm leading-[21px] text-white/85">
          <Info className="mt-0.5 size-4 shrink-0 text-haze" strokeWidth={1.7} aria-hidden="true" />
          <span>{approval.reason}</span>
        </p>
      )}

      {warned && (
        <ul className="m-0 flex list-none flex-col gap-2 p-0" aria-label="Warnings">
          {approval.suspicious_recipients.map((recipient) => (
            <li key={recipient.value + recipient.source} className="flex gap-2.5 rounded-xl border border-busy/45 bg-busy/8 px-3 py-2.5 text-sm leading-[21px] text-white">
              <TriangleAlert className="mt-0.5 size-4 shrink-0 text-busy" strokeWidth={1.7} aria-hidden="true" />
              <span>
                <span className="sr-only">Warning: </span>
                <b className="font-mono font-semibold break-words text-busy">{recipient.value}</b> was not written by you (it came from{' '}
                <span className="font-mono break-words">{recipient.source}</span>)
              </span>
            </li>
          ))}
          {approval.untrusted_sources.length > 0 && approval.suspicious_recipients.length === 0 && (
            <li className="flex gap-2.5 rounded-xl border border-busy/45 bg-busy/8 px-3 py-2.5 text-sm leading-[21px] text-white">
              <TriangleAlert className="mt-0.5 size-4 shrink-0 text-busy" strokeWidth={1.7} aria-hidden="true" />
              <span>
                <span className="sr-only">Warning: </span>
                This run read third-party content first: <span className="font-mono break-words">{approval.untrusted_sources.join(', ')}</span>
              </span>
            </li>
          )}
        </ul>
      )}

      {problem && <p className="m-0 text-sm text-off" role="alert">{problem}</p>}

      <footer className="flex flex-wrap items-center justify-between gap-2">
        {approval.run_id && !compact ? (
          <a href={href('runs', approval.run_id)} className="inline-flex items-center gap-1 rounded-md text-[13px] text-white/65 hover:text-white">
            Open the run
            <ArrowUpRight className="size-3.5" strokeWidth={1.7} aria-hidden="true" />
          </a>
        ) : <span />}
        {pending && (
          <div className="flex gap-2">
            <Button variant="glass" size="sm" onClick={() => decide('deny')} loading={deciding === 'deny'} disabled={deciding !== null}>
              <X />
              Deny
            </Button>
            <Button variant="white" size="sm" onClick={() => decide('approve')} loading={deciding === 'approve'} disabled={deciding !== null}>
              <Check />
              {warned ? 'Approve anyway' : 'Approve'}
            </Button>
          </div>
        )}
      </footer>
    </article>
  )
}
