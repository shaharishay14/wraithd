import { useMemo, useState } from 'react'
import { ArrowLeft, FileText, Search, ShieldAlert } from 'lucide-react'

import { api } from '../../api/client'
import type { NoteListItem } from '../../api/types'
import { cn } from '../../lib/cn'
import { formatExact, formatWhen } from '../../lib/format'
import { href } from '../../lib/router'
import { useResource } from '../../lib/useResource'
import { Badge } from '../../ui/Badge'
import { PageBody, PageHeader } from '../../ui/PageHeader'
import { EmptyState, ErrorState, Loading, panelClass } from '../../ui/Panel'
import { NewNote } from './NewNote'
import { NoteView } from './NoteView'

export default function Memory({ path }: { path: string | null }) {
  const index = useResource(() => api.memory(), [])
  const [query, setQuery] = useState('')
  const notes = useMemo(() => index.data?.notes ?? [], [index.data])

  const byFolder = useMemo(() => {
    const q = query.trim().toLowerCase()
    const groups = new Map<string, NoteListItem[]>()
    for (const folder of Object.keys(index.data?.folders ?? {})) groups.set(folder, [])
    for (const note of notes) {
      if (q && !`${note.title} ${note.path}`.toLowerCase().includes(q)) continue
      const folder = note.path.split('/')[0]
      groups.set(folder, [...(groups.get(folder) ?? []), note])
    }
    return [...groups.entries()]
  }, [index.data, notes, query])

  const lowTrust = notes.filter((n) => n.trust === 'low').length

  return (
    <>
      <PageHeader
        eyebrow="Memory"
        title="What your agent remembers"
        subtitle={
          <>
            Markdown files in <code className="font-mono text-[0.9em]">workspace/memory/</code>. Edit them here or in any editor; the agent sees the change on its next run.
            {lowTrust > 0 && ` ${lowTrust} saved after reading third-party content: check and save them to vouch for them.`}
          </>
        }
      />
      <PageBody>
        {index.loading && <Loading label="Loading memory" />}
        {index.error && !index.data && <ErrorState message={index.error} onRetry={index.reload} />}
        {index.data && (
          <div className="grid gap-4 lg:grid-cols-[300px_minmax(0,1fr)]">
            <nav aria-label="Notes" className={cn('flex flex-col gap-3', path && 'hidden lg:flex')}>
              <NewNote folders={Object.keys(index.data.folders)} existing={notes.map((n) => n.path)} onCreated={index.reload} />
              <div className="relative">
                <Search className="pointer-events-none absolute top-1/2 left-3.5 size-4 -translate-y-1/2 text-white/45" strokeWidth={1.7} aria-hidden="true" />
                <label htmlFor="note-search" className="sr-only">Find a note</label>
                <input
                  id="note-search"
                  type="search"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="Find a note"
                  className="h-10 w-full rounded-pill border border-white/16 bg-night-2 ps-10 pe-4 text-sm text-white placeholder:text-white/45 focus:border-white/50 focus:outline-none"
                />
              </div>
              {byFolder.map(([folder, items]) => (
                <section key={folder} className={cn(panelClass, 'p-3')} aria-labelledby={`folder-${folder}`}>
                  <h2 id={`folder-${folder}`} className="m-0 px-2 text-sm font-semibold" title={index.data!.folders[folder]}>
                    {folder}/ <span className="font-normal text-white/45">{items.length}</span>
                  </h2>
                  <p className="m-0 mb-1.5 px-2 text-xs text-white/50">{index.data!.folders[folder]}</p>
                  {items.length === 0 ? (
                    <p className="m-0 px-2 py-1 text-xs text-white/40">{query ? 'No match' : 'Empty'}</p>
                  ) : (
                    <ul className="m-0 flex list-none flex-col p-0">
                      {items.map((note) => (
                        <li key={note.path}>
                          <a
                            href={href('memory', ...note.path.split('/'))}
                            aria-current={note.path === path ? 'page' : undefined}
                            className={cn('flex items-center gap-2 rounded-lg px-2 py-1.5 text-sm no-underline', note.path === path ? 'bg-white/10 text-white' : 'text-white/80 hover:bg-white/5')}
                          >
                            <FileText className="size-3.5 shrink-0 text-white/45" strokeWidth={1.7} aria-hidden="true" />
                            <span className="min-w-0 flex-1 truncate">{note.title}</span>
                            {note.trust === 'low' && (
                              <span title="Saved after reading third-party content" className="inline-flex items-center">
                                <ShieldAlert className="size-3.5 text-busy" strokeWidth={1.7} aria-hidden="true" />
                                <span className="sr-only">(low trust)</span>
                              </span>
                            )}
                            <span className="text-[11px] text-white/40" title={formatExact(note.updated_at)}>{formatWhen(note.updated_at)}</span>
                          </a>
                        </li>
                      ))}
                    </ul>
                  )}
                </section>
              ))}
            </nav>

            <div className={cn(!path && 'hidden lg:block')}>
              {path ? (
                <>
                  <a href={href('memory')} className="mb-3 inline-flex items-center gap-1.5 text-sm text-white/65 no-underline hover:text-white lg:hidden">
                    <ArrowLeft className="size-4" strokeWidth={1.7} aria-hidden="true" />
                    All notes
                  </a>
                  <NoteView key={path} path={path} notes={notes} onChanged={index.reload} />
                </>
              ) : (
                <EmptyState title={notes.length ? 'Pick a note to read it.' : 'Nothing remembered yet.'}>
                  {notes.length ? <>Notes marked <Badge tone="warn">low trust</Badge> were saved after the agent read third-party content.</> : 'Tell your agent about yourself, and what it learns lands here.'}
                </EmptyState>
              )}
            </div>
          </div>
        )}
      </PageBody>
    </>
  )
}
