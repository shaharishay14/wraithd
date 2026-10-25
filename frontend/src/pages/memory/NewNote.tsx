import { useState, type FormEvent } from 'react'
import { Plus } from 'lucide-react'

import { api, errorMessage } from '../../api/client'
import { navigate } from '../../lib/router'
import { Button } from '../../ui/Button'
import { panelClass } from '../../ui/Panel'

// "Garden plans" -> garden-plans
const slug = (title: string) => title.toLowerCase().trim().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 60)

// A new note is a PUT to a path that doesn't exist yet (docs/api.md, Memory)
export function NewNote({ folders, existing, onCreated }: { folders: string[]; existing: string[]; onCreated: () => void }) {
  const [open, setOpen] = useState(false)
  const [folder, setFolder] = useState(folders[0] ?? '')
  const [title, setTitle] = useState('')
  const [busy, setBusy] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)
  const name = slug(title)
  const path = `${folder}/${name}.md`
  const taken = existing.includes(path)

  const create = async (e: FormEvent) => {
    e.preventDefault()
    if (!name || taken) return
    setBusy(true)
    setProblem(null)
    try {
      await api.saveNote(path, '', title.trim())
      onCreated()
      setOpen(false)
      setTitle('')
      navigate('memory', folder, `${name}.md`)
    } catch (err) {
      setProblem(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  if (!open) {
    return (
      <Button variant="glass" size="sm" onClick={() => setOpen(true)} className="self-start">
        <Plus />
        New note
      </Button>
    )
  }
  const field = 'h-9 rounded-pill border border-white/20 bg-night/60 px-3 text-sm text-white focus:border-white/55 focus:outline-none'
  return (
    <form onSubmit={create} className={`${panelClass} flex flex-col gap-2.5 p-3`} aria-label="New note">
      <div className="flex gap-2">
        <label htmlFor="new-note-folder" className="sr-only">Folder</label>
        <select id="new-note-folder" value={folder} onChange={(e) => setFolder(e.target.value)} className={`${field} shrink-0`}>
          {folders.map((f) => <option key={f} value={f}>{f}/</option>)}
        </select>
        <label htmlFor="new-note-title" className="sr-only">Title</label>
        <input id="new-note-title" autoFocus value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Title" className={`${field} min-w-0 flex-1 placeholder:text-white/40`} />
      </div>
      <p className="m-0 px-1 font-mono text-xs text-white/50">{name ? path : 'Give it a title'}{taken && <span className="text-off"> already exists</span>}</p>
      {problem && <p className="m-0 px-1 text-sm text-off" role="alert">{problem}</p>}
      <div className="flex justify-end gap-2">
        <Button variant="ghost" size="sm" onClick={() => setOpen(false)} disabled={busy}>Cancel</Button>
        <Button type="submit" size="sm" loading={busy} disabled={!name || taken}>Create</Button>
      </div>
    </form>
  )
}
