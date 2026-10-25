import { useState } from 'react'
import { ChevronDown, Globe, KeyRound, Send, Terminal } from 'lucide-react'

import { api, errorMessage } from '../../api/client'
import type { McpServer, McpTool, Pairing, ToolTier } from '../../api/types'
import { cn } from '../../lib/cn'
import { formatExact } from '../../lib/format'
import { useNow, useResource } from '../../lib/useResource'
import { Badge } from '../../ui/Badge'
import { Button } from '../../ui/Button'
import { PageBody, PageHeader } from '../../ui/PageHeader'
import { Code, EmptyState, ErrorState, Loading, Panel, panelClass } from '../../ui/Panel'
import { Spinner } from '../../ui/Spinner'

const TIER: Record<ToolTier, { label: string; tone: 'quiet' | 'warn' | 'default' }> = {
  'read-only': { label: 'Read-only: runs', tone: 'quiet' },
  asks: { label: 'Asks first', tone: 'warn' },
  'always allowed': { label: 'Always allowed', tone: 'default' },
}

function ToolRow({ server, tool, onServer }: { server: string; tool: McpTool; onServer: (s: McpServer) => void }) {
  const [busy, setBusy] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)
  const [tier, setTier] = useState(tool.tier)
  const toggle = async () => {
    setBusy(true)
    setProblem(null)
    try {
      const allowing = tier === 'asks'
      onServer(await (allowing ? api.alwaysAllow(server, tool.tool) : api.askAgain(server, tool.tool)))
      setTier(allowing ? 'always allowed' : 'asks')
    } catch (e) {
      setProblem(errorMessage(e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <li className="flex flex-col gap-2 border-t border-white/8 py-3 first:border-t-0 sm:flex-row sm:items-center">
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="font-mono text-[13px] break-words">{tool.registered_name}</span>
        <span className="text-[13px] text-white/60">{tool.description}</span>
        {problem && <span className="text-sm text-off" role="alert">{problem}</span>}
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <Badge tone={TIER[tier].tone}>{TIER[tier].label}</Badge>
        {tier !== 'read-only' && (
          <Button variant="glass" size="sm" onClick={toggle} loading={busy} aria-label={`${tier === 'asks' ? 'Always allow' : 'Ask again for'} ${tool.registered_name}`}>
            {tier === 'asks' ? 'Always allow' : 'Ask again'}
          </Button>
        )}
      </div>
    </li>
  )
}

function Tools({ server, onServer }: { server: McpServer; onServer: (s: McpServer) => void }) {
  const tools = useResource(() => api.mcpTools(server.name), [server.name])
  if (tools.loading) return <div className="py-4"><Spinner label={`Loading ${server.name} tools`} /></div>
  if (tools.error && !tools.data) return <p className="m-0 py-2 text-sm text-off" role="alert">{tools.error}</p>
  if (!tools.data?.length) return <p className="m-0 py-2 text-sm text-white/50">This server offers no tools.</p>
  return (
    <>
      <p className="m-0 mt-1 text-xs text-white/55">
        "Always allow" still asks (or refuses when nobody's there) if a recipient came from third-party content.
      </p>
      <ul className="m-0 list-none p-0">{tools.data.map((t) => <ToolRow key={t.tool} server={server.name} tool={t} onServer={onServer} />)}</ul>
    </>
  )
}

function ServerCard({ server: initial }: { server: McpServer }) {
  const [server, setServer] = useState(initial)
  const [open, setOpen] = useState(false)
  const Icon = server.transport === 'http' ? Globe : Terminal
  return (
    <li className={cn(panelClass, 'p-4 sm:p-5')}>
      <div className="flex items-start gap-3">
        <span className="inline-grid size-10 shrink-0 place-items-center rounded-full border border-white/16 bg-night-3">
          <Icon className="size-4.5" strokeWidth={1.7} aria-hidden="true" />
        </span>
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <span className="flex flex-wrap items-center gap-2">
            <h3 className="m-0 text-[16px] font-semibold">{server.name}</h3>
            {server.connected ? <Badge status="live">Connected</Badge> : <Badge status="off">Not connected</Badge>}
            {server.provides.map((p) => <Badge key={p} tone="quiet">provides {p}</Badge>)}
          </span>
          <span className="font-mono text-xs break-words text-white/55">{server.transport} · {server.target}</span>
          {server.error && <span className="text-sm text-off">{server.error}</span>}
          {server.secrets.length > 0 && (
            <span className="text-xs text-white/50">
              <KeyRound className="me-1.5 inline size-3.5 align-[-2px]" strokeWidth={1.7} aria-hidden="true" />
              Secrets in the keychain: <span className="font-mono">{server.secrets.join(', ')}</span>
            </span>
          )}
        </div>
        {server.connected && (
          <Button variant="ghost" size="sm" onClick={() => setOpen((v) => !v)} aria-expanded={open} aria-label={`${server.name} tools`} className="px-2.5 sm:px-3.5">
            Tools
            <ChevronDown className={cn('transition-transform', open && 'rotate-180')} />
          </Button>
        )}
      </div>
      {open && <div className="mt-3 border-t border-white/8 pt-2"><Tools server={server} onServer={setServer} /></div>}
    </li>
  )
}

function TelegramPanel() {
  const telegram = useResource(() => api.telegram(), [])
  const [pairing, setPairing] = useState<Pairing | null>(null)
  const [busy, setBusy] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)
  const now = useNow(1000)
  const left = pairing ? Math.max(0, Math.round((new Date(pairing.expires_at).getTime() - now) / 1000)) : 0

  const pair = async () => {
    setBusy(true)
    setProblem(null)
    try {
      setPairing(await api.pair())
    } catch (e) {
      setProblem(errorMessage(e))
    } finally {
      setBusy(false)
    }
  }

  const t = telegram.data
  return (
    <Panel title={<span className="flex items-center gap-2"><Send className="size-4" strokeWidth={1.7} aria-hidden="true" />Telegram</span>}
      actions={t && (t.paired ? <Badge status="live">Paired{t.owner ? ` with ${t.owner}` : ''}</Badge> : t.configured ? <Badge status="busy">Not paired</Badge> : <Badge status="idle">Not set up</Badge>)}>
      {telegram.loading && <Spinner label="Loading Telegram" />}
      {telegram.error && !t && <p className="m-0 text-sm text-off" role="alert">{telegram.error}</p>}
      {t && !t.configured && (
        <p className="m-0 text-sm text-white/65">
          To talk to your agent from Telegram, store your bot's token with <Code>local-agent key set telegram</Code>, restart{' '}
          <Code>local-agent daemon</Code>, then pair from this page.
        </p>
      )}
      {t?.configured && (
        <div className="flex flex-col gap-3 text-sm text-white/70">
          <p className="m-0">Only the paired chat is you; anyone else's message is treated as third-party content. A new code replaces the old pairing.</p>
          {pairing && left > 0 ? (
            <div className="flex flex-wrap items-center gap-3 rounded-xl border border-white/16 bg-night/60 px-4 py-3" aria-live="polite">
              <span>Send this to your bot:</span>
              <Code className="text-[15px] tracking-wider">/pair {pairing.code}</Code>
              <span className="text-xs text-white/50 tabular-nums" title={formatExact(pairing.expires_at)}>expires in {Math.floor(left / 60)}:{String(left % 60).padStart(2, '0')}</span>
            </div>
          ) : (
            <Button variant="glass" size="sm" onClick={pair} loading={busy} className="self-start">{t.paired ? 'Pair a different chat' : 'Get a pairing code'}</Button>
          )}
          {problem && <p className="m-0 text-sm text-off" role="alert">{problem}</p>}
        </div>
      )}
    </Panel>
  )
}

export default function Apps() {
  const servers = useResource(() => api.mcpServers(), [])
  return (
    <>
      <PageHeader
        eyebrow="Apps"
        title="Connected apps"
        subtitle="MCP servers give your agent tools, like email or calendar. Their output is third-party content, and anything that isn't read-only asks first."
      />
      <PageBody>
        <section aria-labelledby="servers-heading" className="mb-8">
          <h2 id="servers-heading" className="m-0 mb-3 font-display text-lg font-medium tracking-[-0.015em]">MCP servers</h2>
          {servers.loading && <Loading label="Loading servers" />}
          {servers.error && !servers.data && <ErrorState message={servers.error} onRetry={servers.reload} />}
          {servers.data?.length === 0 && (
            <EmptyState title="No apps connected.">Add one in the terminal (it keeps the secrets in your keychain): <Code>local-agent mcp add gmail --provides email -- npx -y …</Code></EmptyState>
          )}
          {servers.data && servers.data.length > 0 && (
            <>
              <ul className="m-0 flex list-none flex-col gap-3 p-0">{servers.data.map((s) => <ServerCard key={s.name} server={s} />)}</ul>
              <p className="m-0 mt-3 text-xs text-white/50">Add or remove servers in the terminal: <Code>local-agent mcp add</Code>, <Code>local-agent mcp remove</Code>.</p>
            </>
          )}
        </section>
        <TelegramPanel />
      </PageBody>
    </>
  )
}
