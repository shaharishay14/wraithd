import type { HTMLAttributes } from 'react'

import { cn } from '../lib/cn'
import { StatusDot, type Status } from './StatusDot'

// A small pill. Pass `status` to lead with a status dot.
export interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  status?: Status
  pulse?: boolean
  tone?: 'default' | 'warn' | 'quiet'
}

export function Badge({ status, pulse, tone = 'default', className, children, ...props }: BadgeProps) {
  return (
    <span
      className={cn(
        'inline-flex h-6 shrink-0 items-center gap-1.5 rounded-pill border px-2.5 text-xs leading-none font-medium whitespace-nowrap',
        '[&_svg]:size-3 [&_svg]:stroke-[1.7]',
        tone === 'warn' && 'border-busy/45 bg-busy/10 text-busy',
        tone === 'quiet' && 'border-white/14 bg-transparent text-white/70',
        tone === 'default' && 'border-white/25 bg-white/8 text-white',
        className,
      )}
      {...props}
    >
      {status && <StatusDot status={status} pulse={pulse} />}
      {children}
    </span>
  )
}
