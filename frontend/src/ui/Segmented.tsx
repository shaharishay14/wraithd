import { cn } from '../lib/cn'

// A row of pills, one pressed. Arrow keys aren't needed: each pill is a plain toggle button.
export function Segmented<T extends string>({ items, value, onChange, label }: {
  items: { value: T; label: string; count?: number }[]
  value: T
  onChange: (value: T) => void
  label: string
}) {
  return (
    <div role="group" aria-label={label} className="scrollbar-none inline-flex max-w-full gap-1 overflow-x-auto rounded-pill border border-white/16 bg-night/50 p-1">
      {items.map((item) => (
        <button
          key={item.value}
          type="button"
          aria-pressed={item.value === value}
          onClick={() => onChange(item.value)}
          className={cn(
            'h-7 shrink-0 rounded-pill border px-3 text-[13px] font-medium whitespace-nowrap transition-colors duration-150',
            'focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-white',
            item.value === value
              ? 'border-white/80 bg-white/10 text-white shadow-[0_0_14px_rgba(230,176,109,0.18)]'
              : 'border-transparent text-white/65 hover:text-white',
          )}
        >
          {item.label}
          {item.count != null && <span className="ms-1.5 text-white/50 tabular-nums">{item.count}</span>}
        </button>
      ))}
    </div>
  )
}
