import { cn } from '../lib/cn'

// live = green, busy = needs attention (amber), off = failed or disconnected (coral), idle = grey
export type Status = 'live' | 'busy' | 'off' | 'idle'

export function StatusDot({ status = 'idle', pulse = false, className }: { status?: Status; pulse?: boolean; className?: string }) {
  return <span className={cn('sky-dot', className)} data-status={status} data-pulse={pulse} aria-hidden="true" />
}
