import { useState } from 'react'
import { Plus, X } from 'lucide-react'

import { api, errorMessage } from '../../api/client'
import { useResource } from '../../lib/useResource'
import { Badge } from '../../ui/Badge'
import { Button, IconButton } from '../../ui/Button'
import { ErrorState, Loading, Panel, Rows, None } from '../../ui/Panel'

const MAX_RULE = 300

export function SkillDetail({ name, onChanged }: { name: string; onChanged: () => void }) {
  const skill = useResource(() => api.skill(name), [name])
  const [rules, setRules] = useState<string[] | null>(null)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)

  if (skill.loading) return <Loading label="Loading the skill" />
  if (skill.error && !skill.data) return <ErrorState message={skill.error} onRetry={skill.reload} />
  const s = skill.data!
  const editing = rules !== null
  const shown = rules ?? s.adjustments

  const save = async () => {
    if (!rules) return
    setBusy(true)
    setProblem(null)
    try {
      const updated = await api.setAdjustments(name, rules)
      skill.setData({ ...s, ...updated })
      setRules(null)
      onChanged()
    } catch (e) {
      setProblem(errorMessage(e))
    } finally {
      setBusy(false)
    }
  }
  const add = () => {
    const rule = draft.trim()
    if (!rule) return
    setRules([...shown, rule])
    setDraft('')
  }

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_340px]">
      <div className="flex flex-col gap-4">
        <Panel title="What it's for">
          <p className="m-0 text-white/80">{s.description}</p>
        </Panel>
        <Panel title="Its steps">
          <pre className="m-0 overflow-auto font-mono text-[13px] leading-6 whitespace-pre-wrap text-white/88">{s.instructions}</pre>
        </Panel>
        <Panel title="Your rules" actions={!editing && <Button variant="glass" size="sm" onClick={() => setRules(s.adjustments)}>Edit rules</Button>}>
          <p className="m-0 mb-3 text-sm text-white/60">Shown to the agent after the steps, every time it uses this skill. One line each.</p>
          {shown.length === 0 && !editing && <p className="m-0 text-sm text-white/45">No rules yet. Correct the agent in chat ("always put Slack first") and it offers to save one.</p>}
          {shown.length > 0 && (
            <ul className="m-0 flex list-none flex-col gap-1.5 p-0">
              {shown.map((rule, i) => (
                <li key={i} className="flex items-center gap-2 rounded-xl border border-white/10 bg-night/50 px-3 py-1.5 text-sm">
                  <span className="min-w-0 flex-1 break-words">{rule}</span>
                  {editing && (
                    <IconButton aria-label={`Remove rule: ${rule}`} onClick={() => setRules(shown.filter((_, j) => j !== i))} className="size-7">
                      <X />
                    </IconButton>
                  )}
                </li>
              ))}
            </ul>
          )}
          {editing && (
            <div className="mt-3 flex flex-col gap-3">
              <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); add() }}>
                <label htmlFor="new-rule" className="sr-only">New rule</label>
                <input
                  id="new-rule"
                  value={draft}
                  maxLength={MAX_RULE}
                  onChange={(e) => setDraft(e.target.value.replace(/\n/g, ' '))}
                  placeholder="A new rule"
                  className="h-9 min-w-0 flex-1 rounded-pill border border-white/20 bg-night/60 px-4 text-sm text-white placeholder:text-white/40 focus:border-white/55 focus:outline-none"
                />
                <Button type="submit" variant="glass" size="sm" disabled={!draft.trim()}><Plus />Add</Button>
              </form>
              {problem && <p className="m-0 text-sm text-off" role="alert">{problem}</p>}
              <div className="flex justify-end gap-2">
                <Button variant="ghost" size="sm" onClick={() => { setRules(null); setDraft('') }} disabled={busy}>Cancel</Button>
                <Button size="sm" onClick={save} loading={busy}>Save rules</Button>
              </div>
            </div>
          )}
        </Panel>
      </div>
      <Panel title="Details" className="self-start">
        <Rows
          rows={[
            ['Status', s.available ? <Badge status="live">Available</Badge> : <Badge status="off">Not offered</Badge>],
            ['Needs', s.requires.length ? <span className="font-mono text-[13px]">{s.requires.join(', ')}</span> : <None />],
            ['Missing', s.missing.length ? <span className="font-mono text-[13px] text-off">{s.missing.join(', ')}</span> : <None />],
            ['From', s.source === 'builtin' ? 'Built in' : 'Your skills folder'],
            ['Model', s.model === 'strong' ? 'Switches to the strong model' : 'Default'],
            ['Files', s.files.length ? <span className="font-mono text-[13px]">{s.files.join(', ')}</span> : <None />],
          ]}
        />
      </Panel>
    </div>
  )
}
