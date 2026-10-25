import { cn } from '../lib/cn'

// Ring spinner, no text. With a label it's announced as a status.
export function Spinner({ size = 22, tone = 'light', label, className }: { size?: number; tone?: 'light' | 'dark'; label?: string; className?: string }) {
  return (
    <span
      role={label ? 'status' : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      className={cn(
        'inline-block shrink-0 animate-sky-spin rounded-full border-solid',
        tone === 'light' ? 'border-white/22 border-t-white' : 'border-on-white/20 border-t-on-white',
        className,
      )}
      style={{ width: size, height: size, borderWidth: Math.max(1.5, Math.round(size / 12)) }}
    />
  )
}
