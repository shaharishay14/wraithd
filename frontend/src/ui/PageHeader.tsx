import type { ReactNode } from 'react'

// The top of every page: a dusk glow fading into night, title left, actions right
export function PageHeader({ eyebrow, title, subtitle, actions }: { eyebrow?: string; title: ReactNode; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <header className="sky-band border-b border-white/8">
      <div className="mx-auto flex max-w-[1180px] flex-wrap items-end justify-between gap-x-6 gap-y-4 px-4 pt-8 pb-6 sm:px-6 lg:px-8">
        <div className="flex min-w-0 flex-col gap-1.5">
          {eyebrow && <p className="m-0 text-xs tracking-[0.08em] text-white/70 uppercase">{eyebrow}</p>}
          <h1 className="m-0 font-display text-[28px] leading-9 font-medium tracking-[-0.03em] break-words sm:text-[32px] sm:leading-10">{title}</h1>
          {subtitle && <div className="m-0 max-w-[70ch] text-white/70">{subtitle}</div>}
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
      </div>
    </header>
  )
}

export function PageBody({ children }: { children: ReactNode }) {
  return <div className="mx-auto max-w-[1180px] px-4 pt-6 pb-16 sm:px-6 lg:px-8">{children}</div>
}
