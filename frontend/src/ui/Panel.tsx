import type { ReactNode } from 'react'

import { cn } from '../lib/cn'
import { Button } from './Button'
import { Spinner } from './Spinner'

export const panelClass = 'rounded-card border border-white/12 bg-night-2 shadow-[inset_0_1px_0_rgba(255,255,255,0.05)]'

// A solid night panel with an optional heading
export function Panel({ title, actions, className, children, as: Tag = 'section' }: {
  title?: ReactNode
  actions?: ReactNode
  className?: string
  children: ReactNode
  as?: 'section' | 'div' | 'article'
}) {
  return (
    <Tag className={cn(panelClass, 'p-5', className)}>
      {(title || actions) && (
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          {title && <h2 className="m-0 font-display text-[17px] leading-6 font-medium tracking-[-0.015em]">{title}</h2>}
          {actions}
        </div>
      )}
      {children}
    </Tag>
  )
}

export function Loading({ label }: { label: string }) {
  return (
    <div className="grid min-h-[220px] place-items-center">
      <Spinner label={label} />
    </div>
  )
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className={cn(panelClass, 'flex min-h-[200px] flex-col items-center justify-center gap-4 p-8 text-center')} role="alert">
      <p className="m-0 text-white/75">{message}</p>
      {onRetry && (
        <Button variant="glass" size="sm" onClick={onRetry}>
          Try again
        </Button>
      )}
    </div>
  )
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className={cn(panelClass, 'flex min-h-[180px] flex-col items-center justify-center gap-2 p-8 text-center')} role="status">
      <p className="m-0 text-white/85">{title}</p>
      {children && <div className="m-0 max-w-[52ch] text-sm text-white/55">{children}</div>}
    </div>
  )
}

// Label / value rows
export function Rows({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="m-0 flex flex-col gap-2 text-sm">
      {rows.map(([label, value]) => (
        <div key={label} className="flex items-baseline justify-between gap-4">
          <dt className="shrink-0 text-white/60">{label}</dt>
          <dd className="m-0 min-w-0 text-right break-words text-white/90">{value}</dd>
        </div>
      ))}
    </dl>
  )
}

export function None() {
  return <span className="text-white/45">None</span>
}

export function Code({ children, className }: { children: ReactNode; className?: string }) {
  return <code className={cn('rounded-md bg-white/8 px-1.5 py-0.5 font-mono text-[0.86em] text-white/90', className)}>{children}</code>
}
