import { useEffect, useRef, useState } from 'react'
import { Activity, AppWindow, CalendarClock, KeyRound, MessageSquare, NotebookPen, ShieldCheck, Sparkles } from 'lucide-react'

import { onUnauthorized } from './api/client'
import { cn } from './lib/cn'
import { usePendingApprovals } from './lib/pending'
import { href, useRoute } from './lib/router'
import Apps from './pages/apps/Apps'
import Approvals from './pages/approvals/Approvals'
import Chat from './pages/chat/Chat'
import { useChat } from './pages/chat/store'
import Memory from './pages/memory/Memory'
import RunDetail from './pages/runs/RunDetail'
import Runs from './pages/runs/Runs'
import Schedules from './pages/schedules/Schedules'
import Skills from './pages/skills/Skills'

const NAV = [
  { key: 'chat', label: 'Chat', icon: MessageSquare },
  { key: 'approvals', label: 'Approvals', icon: ShieldCheck },
  { key: 'runs', label: 'Runs', icon: Activity },
  { key: 'memory', label: 'Memory', icon: NotebookPen },
  { key: 'skills', label: 'Skills', icon: Sparkles },
  { key: 'schedules', label: 'Schedules', icon: CalendarClock },
  { key: 'apps', label: 'Apps', icon: AppWindow },
] as const

function Logo() {
  return (
    <a href={href('chat')} className="inline-flex items-center gap-2.5 rounded-lg font-display text-[16px] font-semibold tracking-[-0.02em] text-white no-underline">
      <span className="inline-grid size-7 place-items-center rounded-lg bg-white text-night" aria-hidden="true">
        <svg viewBox="0 0 24 24" className="size-4" fill="currentColor">
          <circle cx="7.5" cy="7.5" r="5" />
          <rect x="12" y="12" width="10" height="10" rx="2.6" />
          <path d="M9 9l5 5" stroke="currentColor" strokeWidth="3.4" strokeLinecap="round" />
        </svg>
      </span>
      Local Agent
    </a>
  )
}

function Nav({ active, pending, row = false }: { active: string; pending: number; row?: boolean }) {
  const ref = useRef<HTMLElement>(null)
  // Chat returns to the open conversation, not a new one
  const conversationId = useChat().conversationId
  // The phone's tab row scrolls: keep the current page's tab in view
  useEffect(() => {
    if (row) ref.current?.querySelector('[aria-current="page"]')?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
  }, [row, active])
  return (
    <nav aria-label="Main" ref={ref}>
      <ul className={cn('m-0 flex list-none p-0', row ? 'scrollbar-none gap-1 overflow-x-auto px-4 pb-2.5' : 'flex-col gap-0.5')}>
        {NAV.map(({ key, label, icon: Icon }) => {
          const current = active === key
          return (
            <li key={key} className="shrink-0">
              <a
                href={key === 'chat' && conversationId ? href('chat', conversationId) : href(key)}
                aria-current={current ? 'page' : undefined}
                className={cn(
                  'flex items-center gap-2.5 rounded-pill text-sm font-medium no-underline transition-colors duration-150',
                  row ? 'h-8 border px-3' : 'h-9 px-3',
                  current
                    ? row ? 'border-white/80 bg-white/10 text-white' : 'bg-white/10 text-white shadow-[inset_0_0_0_1px_rgba(255,255,255,0.18)]'
                    : cn('text-white/65 hover:bg-white/6 hover:text-white', row && 'border-white/12'),
                )}
              >
                <Icon className="size-4 shrink-0" strokeWidth={1.7} aria-hidden="true" />
                {label}
                {key === 'approvals' && pending > 0 && (
                  <span className="ms-auto inline-grid h-5 min-w-5 place-items-center rounded-pill bg-busy px-1.5 text-[11px] font-semibold text-on-white tabular-nums">
                    {pending}
                    <span className="sr-only"> waiting</span>
                  </span>
                )}
              </a>
            </li>
          )
        })}
      </ul>
    </nav>
  )
}

function Unauthorized() {
  return (
    <main className="grid min-h-dvh place-items-center bg-night px-4">
      <div className="flex max-w-[440px] flex-col items-center gap-4 rounded-card border border-white/12 bg-night-2 p-8 text-center" role="alert">
        <span className="inline-grid size-11 place-items-center rounded-full border border-white/20 bg-white/6">
          <KeyRound className="size-5" strokeWidth={1.7} aria-hidden="true" />
        </span>
        <h1 className="m-0 font-display text-2xl font-medium tracking-[-0.025em]">This page needs its key</h1>
        <p className="m-0 text-white/72">
          Open the link printed by <code className="rounded-md bg-white/8 px-1.5 py-0.5 font-mono text-[0.9em]">local-agent daemon</code>. It carries the token that lets this
          page talk to your agent.
        </p>
      </div>
    </main>
  )
}

function Page({ route }: { route: string[] }) {
  const [page, id] = route
  switch (page) {
    case 'approvals':
      return <Approvals />
    case 'runs':
      return id ? <RunDetail runId={id} /> : <Runs />
    case 'memory':
      return <Memory path={route.slice(1).join('/') || null} />
    case 'skills':
      return <Skills name={id ?? null} />
    case 'schedules':
      return <Schedules />
    case 'apps':
      return <Apps />
    default:
      return <Chat conversationId={page === 'chat' ? (id ?? null) : null} />
  }
}

export default function App({ mock }: { mock: boolean }) {
  const route = useRoute()
  const [unauthorized, setUnauthorized] = useState(false)
  const pending = usePendingApprovals().length
  const active = NAV.some((item) => item.key === route[0]) ? route[0] : 'chat'

  useEffect(() => onUnauthorized(() => setUnauthorized(true)), [])
  useEffect(() => {
    const label = NAV.find((item) => item.key === active)?.label
    document.title = `${pending ? `(${pending}) ` : ''}${label} · Local Agent`
  }, [active, pending])

  if (unauthorized) return <Unauthorized />

  return (
    <div className="min-h-dvh bg-night text-white md:grid md:grid-cols-[224px_minmax(0,1fr)]">
      <a href="#main" className="skip-link" onClick={(e) => { e.preventDefault(); document.getElementById('main')?.focus() }}>
        Skip to content
      </a>

      <aside className="hidden border-e border-white/8 bg-side md:sticky md:top-0 md:flex md:h-dvh md:flex-col md:gap-6 md:px-3 md:py-5">
        <div className="px-2">
          <Logo />
        </div>
        <Nav active={active} pending={pending} />
        {mock && <MockNote />}
      </aside>

      <div className="sticky top-0 z-20 border-b border-white/8 bg-side/95 backdrop-blur-md md:hidden">
        <div className="flex h-13 items-center justify-between px-4">
          <Logo />
          {mock && <span className="rounded-pill border border-white/20 px-2 py-0.5 text-[11px] text-white/70">Mock data</span>}
        </div>
        <Nav active={active} pending={pending} row />
      </div>

      <main id="main" tabIndex={-1} className="min-w-0 focus:outline-none">
        <Page route={route} />
      </main>
    </div>
  )
}

function MockNote() {
  return (
    <p className="m-0 mt-auto rounded-2xl border border-white/12 px-3 py-2.5 text-xs leading-[18px] text-white/60">
      <span className="font-semibold text-white/85">Mock data.</span> No daemon or model: replies, runs and notes come from the built-in mock.
    </p>
  )
}
