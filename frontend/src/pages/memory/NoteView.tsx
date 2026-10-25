import { Fragment, useState } from 'react'
import { Pencil, ShieldAlert, Trash2 } from 'lucide-react'

import { api, errorMessage } from '../../api/client'
import type { NoteListItem } from '../../api/types'
import { cn } from '../../lib/cn'
import { formatExact } from '../../lib/format'
import { href, navigate } from '../../lib/router'
import { useResource } from '../../lib/useResource'
import { Badge } from '../../ui/Badge'
import { Button } from '../../ui/Button'
import { ErrorState, Loading, panelClass } from '../../ui/Panel'
import { SOURCE_LABEL } from './labels'

const stem = (path: string) => path.split('/').pop()!.replace(/\.md$/, '')

// The body as written, with [[links]] to other notes made clickable
function Body({ text, notes }: { text: string; notes: NoteListItem[] }) {
  const parts = text.split(/(\[\[[^\]]+\]\])/g)
  return (
    <div className="font-mono text-[13.5px] leading-6 break-words whitespace-pre-wrap text-white/90">
      {parts.map((part, i) => {
        const link = /^\[\[([^\]]+)\]\]$/.exec(part)
        if (!link) return <Fragment key={i}>{part}</Fragment>
        const target = notes.find((n) => stem(n.path) === link[1] || n.path === link[1])
        return target ? (
          <a key={i} href={href('memory', ...target.path.split('/'))} className="text-haze underline decoration-haze/40 underline-offset-2 hover:decoration-haze">{part}</a>
        ) : (
          <span key={i} className="text-white/55" title="No note by that name">{part}</span>
        )
      })}
    </div>
  )
}

export function NoteView({ path, notes, onChanged }: { path: string; notes: NoteListItem[]; onChanged: () => void }) {
  const note = useResource(() => api.note(path), [path])
  const [editing, setEditing] = useState<string | null>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [busy, setBusy] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)

  if (note.loading) return <Loading label="Loading the note" />
  if (note.error && !note.data) return <ErrorState message={note.error} onRetry={note.reload} />
  const n = note.data!

  const save = async () => {
    if (editing === null) return
    setBusy(true)
    setProblem(null)
    try {
      note.setData(await api.saveNote(path, editing))
      setEditing(null)
      onChanged()
    } catch (e) {
      setProblem(errorMessage(e))
    } finally {
      setBusy(false)
    }
  }
  const remove = async () => {
    setBusy(true)
    setProblem(null)
    try {
      await api.deleteNote(path)
      onChanged()
      navigate('memory')
    } catch (e) {
      setProblem(errorMessage(e))
      setBusy(false)
    }
  }
  const vouch = async () => {
    setBusy(true)
    setProblem(null)
    try {
      note.setData(await api.saveNote(path, n.body))
      onChanged()
    } catch (e) {
      setProblem(errorMessage(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <article className={cn(panelClass, 'flex flex-col gap-4 p-4 sm:p-6')} aria-labelledby="note-title">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-1">
          <h2 id="note-title" className="m-0 font-display text-[24px] leading-8 font-medium tracking-[-0.025em] break-words">{n.title}</h2>
          <p className="m-0 font-mono text-xs text-white/50">{n.path} · updated {formatExact(n.updated_at)}</p>
        </div>
        <div className="flex flex-wrap gap-1.5">
          {n.trust === 'low' ? <Badge tone="warn"><ShieldAlert />Low trust</Badge> : <Badge status="live">Trusted</Badge>}
          <Badge tone="quiet">{SOURCE_LABEL[n.source]}</Badge>
        </div>
      </header>

      {n.trust === 'low' && (
        <div className="flex flex-col gap-2 rounded-xl border border-busy/45 bg-busy/8 px-3.5 py-3 text-sm leading-[21px] sm:flex-row sm:items-center">
          <ShieldAlert className="size-4 shrink-0 text-busy" strokeWidth={1.7} aria-hidden="true" />
          <p className="m-0 flex-1">
            Saved after the agent read third-party content{n.source_detail && <> (<span className="font-mono text-[13px]">{n.source_detail}</span>)</>}. The agent treats it with care until you vouch for it.
          </p>
          {editing === null && <Button variant="glass" size="sm" onClick={vouch} loading={busy}>It's right: trust it</Button>}
        </div>
      )}

      {editing === null ? (
        n.body ? <Body text={n.body} notes={notes} /> : <p className="m-0 text-sm text-white/50">Empty note. Edit it to add what your agent should know.</p>
      ) : (
        <div className="flex flex-col gap-2">
          <label htmlFor="note-body" className="text-sm text-white/70">Note text (markdown). Saving makes it your own note, fully trusted.</label>
          <textarea
            id="note-body"
            value={editing}
            onChange={(e) => setEditing(e.target.value)}
            rows={Math.min(18, Math.max(6, editing.split('\n').length + 2))}
            className="w-full rounded-xl border border-white/20 bg-night/70 p-3 font-mono text-[13.5px] leading-6 text-white focus:border-white/55 focus:outline-none"
          />
        </div>
      )}

      {(n.links.length > 0 || n.linked_from.length > 0) && (
        <dl className="m-0 grid gap-3 border-t border-white/8 pt-3 text-sm sm:grid-cols-2">
          {([['Links to', n.links.map((l) => notes.find((x) => stem(x.path) === l)?.path ?? l)], ['Linked from', n.linked_from]] as const).map(([label, items]) => (
            <div key={label}>
              <dt className="mb-1 text-xs text-white/50">{label}</dt>
              <dd className="m-0 flex flex-wrap gap-1.5">
                {items.length === 0 ? <span className="text-white/40">None</span> : items.map((item) =>
                  item.includes('/') ? (
                    <a key={item} href={href('memory', ...item.split('/'))} className="rounded-pill border border-white/16 px-2.5 py-0.5 font-mono text-xs text-white/80 no-underline hover:border-white/40">{item}</a>
                  ) : <span key={item} className="font-mono text-xs text-white/50">{item}</span>)}
              </dd>
            </div>
          ))}
        </dl>
      )}

      {problem && <p className="m-0 text-sm text-off" role="alert">{problem}</p>}

      <footer className="flex flex-wrap items-center justify-end gap-2 border-t border-white/8 pt-3">
        {editing !== null ? (
          <>
            <Button variant="ghost" size="sm" onClick={() => setEditing(null)} disabled={busy}>Cancel</Button>
            <Button size="sm" onClick={save} loading={busy}>Save</Button>
          </>
        ) : confirmDelete ? (
          <>
            <span className="me-auto text-sm text-white/80" role="alert">Delete {n.path}? The file is removed from disk.</span>
            <Button variant="ghost" size="sm" onClick={() => setConfirmDelete(false)} disabled={busy}>Keep it</Button>
            <Button variant="danger" size="sm" onClick={remove} loading={busy}>Delete</Button>
          </>
        ) : (
          <>
            <Button variant="ghost" size="sm" onClick={() => setConfirmDelete(true)}><Trash2 />Delete</Button>
            <Button variant="glass" size="sm" onClick={() => setEditing(n.body)}><Pencil />Edit</Button>
          </>
        )}
      </footer>
    </article>
  )
}
