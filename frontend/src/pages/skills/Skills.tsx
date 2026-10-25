import { useState } from 'react'
import { ArrowLeft, Check, ChevronRight, Lightbulb, X } from 'lucide-react'

import { api, errorMessage } from '../../api/client'
import type { Skill, SkillProposal } from '../../api/types'
import { cn } from '../../lib/cn'
import { href } from '../../lib/router'
import { useResource } from '../../lib/useResource'
import { Badge } from '../../ui/Badge'
import { Button, LinkButton } from '../../ui/Button'
import { PageBody, PageHeader } from '../../ui/PageHeader'
import { EmptyState, ErrorState, Loading, Panel, panelClass } from '../../ui/Panel'
import { SkillDetail } from './SkillDetail'

function Requires({ skill }: { skill: Pick<Skill, 'requires' | 'missing'> }) {
  if (!skill.requires.length) return null
  return (
    <span className="flex flex-wrap gap-1">
      {skill.requires.map((r) => (
        <span key={r} className={cn('rounded-pill border px-2 py-0.5 font-mono text-[11px]', skill.missing.includes(r) ? 'border-off/40 text-off' : 'border-white/14 text-white/65')}>
          {r}
          {skill.missing.includes(r) && <span className="sr-only"> (missing)</span>}
        </span>
      ))}
    </span>
  )
}

function SkillCard({ skill }: { skill: Skill }) {
  return (
    <a href={href('skills', skill.name)} className={cn(panelClass, 'group flex h-full flex-col gap-2 p-4 no-underline transition-colors hover:border-white/30', !skill.available && 'opacity-80')}>
      <span className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-[14px] font-medium text-white">{skill.name}</span>
        {skill.source === 'user' && <Badge tone="quiet">Yours</Badge>}
        {skill.adjustments.length > 0 && <Badge tone="quiet">{skill.adjustments.length} {skill.adjustments.length === 1 ? 'rule' : 'rules'}</Badge>}
        {skill.model === 'strong' && <Badge tone="quiet">Strong model</Badge>}
        <ChevronRight className="ms-auto size-4 text-white/40 transition-transform group-hover:translate-x-0.5" strokeWidth={1.7} aria-hidden="true" />
      </span>
      <span className="line-clamp-3 text-sm text-white/70">{skill.description}</span>
      <span className="mt-auto flex flex-wrap items-center justify-between gap-2 pt-1">
        <Requires skill={skill} />
        {skill.available ? <Badge status="live">Available</Badge> : <Badge status="off">Needs {skill.missing.join(', ')}</Badge>}
      </span>
    </a>
  )
}

function Proposal({ proposal, onDone }: { proposal: SkillProposal; onDone: () => void }) {
  const [busy, setBusy] = useState<'approve' | 'discard' | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const act = async (kind: 'approve' | 'discard') => {
    setBusy(kind)
    setProblem(null)
    try {
      if (kind === 'approve') await api.approveProposal(proposal.name)
      else await api.deleteProposal(proposal.name)
      onDone()
    } catch (e) {
      setProblem(errorMessage(e))
    } finally {
      setBusy(null)
    }
  }
  return (
    <article className="flex flex-col gap-3 rounded-card border border-white/30 bg-night-2 p-4 sm:p-5" aria-labelledby={`proposal-${proposal.name}`}>
      <header className="flex flex-wrap items-center gap-2">
        <Lightbulb className="size-4 text-glow" strokeWidth={1.7} aria-hidden="true" />
        <h3 id={`proposal-${proposal.name}`} className="m-0 font-mono text-[15px] font-medium">{proposal.name}</h3>
        <span className="text-xs text-white/50">proposed by {proposal.proposed_by}</span>
      </header>
      <p className="m-0 text-sm text-white/80">{proposal.description}</p>
      <pre className="m-0 max-h-64 overflow-auto rounded-xl border border-white/8 bg-night/70 p-3 font-mono text-[13px] leading-5 whitespace-pre-wrap text-white/85">{proposal.instructions}</pre>
      {problem && <p className="m-0 text-sm text-off" role="alert">{problem}</p>}
      <footer className="flex flex-wrap items-center justify-between gap-2">
        <Requires skill={{ requires: proposal.requires, missing: [] }} />
        <span className="ms-auto flex gap-2">
          <Button variant="glass" size="sm" onClick={() => act('discard')} loading={busy === 'discard'} disabled={busy !== null}><X />Discard</Button>
          <Button size="sm" onClick={() => act('approve')} loading={busy === 'approve'} disabled={busy !== null}><Check />Turn it on</Button>
        </span>
      </footer>
    </article>
  )
}

export default function Skills({ name }: { name: string | null }) {
  const skills = useResource(() => api.skills(), [])
  const proposals = useResource(() => api.proposals(), [])

  if (name) {
    return (
      <>
        <PageHeader eyebrow="Skill" title={<span className="font-mono text-[0.85em]">{name}</span>} actions={
          <LinkButton variant="glass" size="sm" href={href('skills')}><ArrowLeft />All skills</LinkButton>
        } />
        <PageBody>
          <SkillDetail name={name} onChanged={skills.reload} />
        </PageBody>
      </>
    )
  }

  const available = skills.data?.filter((s) => s.available) ?? []
  const unavailable = skills.data?.filter((s) => !s.available) ?? []

  return (
    <>
      <PageHeader
        eyebrow="Skills"
        title="How your agent does things"
        subtitle="Step-by-step instructions the agent loads when a request matches. A skill shows up once what it needs is connected."
      />
      <PageBody>
        {(proposals.data?.length ?? 0) > 0 && (
          <section aria-labelledby="proposals-heading" className="mb-8">
            <h2 id="proposals-heading" className="m-0 mb-1 font-display text-lg font-medium tracking-[-0.015em]">Proposed by your agent</h2>
            <p className="m-0 mb-3 text-sm text-white/60">Drafts from <span className="font-mono">skill-writer</span>. Nothing changes until you turn one on.</p>
            <div className="grid gap-3 lg:grid-cols-2">
              {proposals.data!.map((p) => <Proposal key={p.name} proposal={p} onDone={() => { void proposals.reload(); void skills.reload() }} />)}
            </div>
          </section>
        )}

        {skills.loading && <Loading label="Loading skills" />}
        {skills.error && !skills.data && <ErrorState message={skills.error} onRetry={skills.reload} />}
        {skills.data && skills.data.length === 0 && <EmptyState title="No skills." />}
        {([['Available', available], ['Waiting for an app', unavailable]] as const).map(([title, list]) => list.length > 0 && (
          <section key={title} aria-label={title} className="mb-8">
            <h2 className="m-0 mb-3 font-display text-lg font-medium tracking-[-0.015em]">{title} <span className="text-sm font-normal text-white/45">{list.length}</span></h2>
            <ul className="m-0 grid list-none gap-3 p-0 sm:grid-cols-2 xl:grid-cols-3">
              {list.map((skill) => <li key={skill.name}><SkillCard skill={skill} /></li>)}
            </ul>
          </section>
        ))}
        {unavailable.length > 0 && (
          <Panel title="Connecting what a skill needs" className="text-sm text-white/70">
            <p className="m-0">
              Add an MCP server that provides it, e.g. <code className="font-mono text-white/90">local-agent mcp add gmail --provides email -- …</code>. See Apps for what's connected.
            </p>
          </Panel>
        )}
      </PageBody>
    </>
  )
}
