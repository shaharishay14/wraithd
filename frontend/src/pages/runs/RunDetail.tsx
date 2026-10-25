import { useEffect } from 'react'
import { ArrowLeft } from 'lucide-react'

import { api } from '../../api/client'
import { formatExact } from '../../lib/format'
import { href } from '../../lib/router'
import { useResource } from '../../lib/useResource'
import { Badge } from '../../ui/Badge'
import { LinkButton } from '../../ui/Button'
import { PageBody, PageHeader } from '../../ui/PageHeader'
import { ErrorState, Loading } from '../../ui/Panel'
import { DisclosurePanel, RunStats, SafetyPanel } from './RunPanels'
import { RunTimeline } from './RunTimeline'
import { SOURCE_LABEL, runBadge } from './utils'

export default function RunDetail({ runId }: { runId: string }) {
  // A running run is polled until it ends
  const run = useResource(() => api.run(runId), [runId])
  const summary = run.data?.summary
  const running = summary?.outcome === 'running'
  const { reload } = run
  useEffect(() => {
    if (!running) return
    const timer = setInterval(() => void reload(), 2000)
    return () => clearInterval(timer)
  }, [running, reload])
  const badge = summary && runBadge(summary.outcome, summary.stop_reason)

  return (
    <>
      <PageHeader
        eyebrow={summary?.source ? `${SOURCE_LABEL[summary.source]} run` : 'Run'}
        title={summary ? summary.message || 'No message' : 'Run'}
        subtitle={summary && (
          <span className="flex flex-wrap gap-x-3 text-sm text-white/55">
            <span>{formatExact(summary.started_at)}</span>
            <span className="font-mono">{runId}</span>
          </span>
        )}
        actions={
          <>
            {badge && <Badge status={badge.status} pulse={summary?.outcome === 'running'}>{badge.label}</Badge>}
            <LinkButton variant="glass" size="sm" href={href('runs')}>
              <ArrowLeft />
              All runs
            </LinkButton>
          </>
        }
      />
      <PageBody>
        {run.loading && <Loading label="Loading the run" />}
        {run.error && !run.data && <ErrorState message={run.error} onRetry={run.reload} />}
        {run.data && summary && (
          <div className="flex flex-col gap-4">
            <RunStats summary={summary} />
            <div className="grid gap-4 lg:grid-cols-2">
              <DisclosurePanel summary={summary} />
              <SafetyPanel summary={summary} />
            </div>
            <RunTimeline events={run.data.events} />
          </div>
        )}
      </PageBody>
    </>
  )
}
