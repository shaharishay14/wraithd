import { useMemo, useState } from 'react'

import { api } from '../../api/client'
import type { RunSource } from '../../api/types'
import { useResource } from '../../lib/useResource'
import { PageBody, PageHeader } from '../../ui/PageHeader'
import { EmptyState, ErrorState, Loading } from '../../ui/Panel'
import { Segmented } from '../../ui/Segmented'
import { RunRow } from './RunRow'

type Filter = 'all' | RunSource | 'flagged'

export default function Runs() {
  const runs = useResource(() => api.runs(100), [], 5000)
  const [filter, setFilter] = useState<Filter>('all')
  const shown = useMemo(
    () => (runs.data ?? []).filter((run) =>
      filter === 'all' ? true : filter === 'flagged' ? Boolean(run.stop_reason || run.untrusted_sources.length || run.outcome !== 'answered') : run.source === filter),
    [runs.data, filter],
  )

  return (
    <>
      <PageHeader
        eyebrow="Runs"
        title="What your agent did"
        subtitle="Every run, step by step: what the agent was given, what it called, and what was blocked or stopped."
        actions={(runs.data?.length ?? 0) > 0 && (
          <Segmented
            label="Filter runs"
            value={filter}
            onChange={setFilter}
            items={[
              { value: 'all', label: 'All' },
              { value: 'chat', label: 'Chat' },
              { value: 'telegram', label: 'Telegram' },
              { value: 'schedule', label: 'Schedules' },
              { value: 'flagged', label: 'Flagged' },
            ]}
          />
        )}
      />
      <PageBody>
        {runs.loading && <Loading label="Loading runs" />}
        {runs.error && !runs.data && <ErrorState message={runs.error} onRetry={runs.reload} />}
        {runs.data && shown.length === 0 && (
          <EmptyState title={runs.data.length ? 'No runs of this kind.' : 'No runs yet.'}>Chat with your agent or give it a schedule, and each run shows up here.</EmptyState>
        )}
        {shown.length > 0 && (
          <ul className="m-0 flex list-none flex-col gap-2 p-0">
            {shown.map((run) => (
              <li key={run.run_id}>
                <RunRow run={run} />
              </li>
            ))}
          </ul>
        )}
      </PageBody>
    </>
  )
}
