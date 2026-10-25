// The local API, contract v1 (docs/api.md on main). Field names are the contract's; don't rename them here.

export interface ApiErrorBody {
  error: { code: ErrorCode; message: string }
}
export type ErrorCode = 'bad_request' | 'unauthorized' | 'bad_origin' | 'bad_host' | 'not_found' | 'conflict' | 'invalid' | 'internal'

// --- status -----------------------------------------------------------------------------------------------

export interface Status {
  version: string
  model: { provider: string; default: string; strong: string | null }
  timezone: string
  shell_enabled: boolean
  search_enabled: boolean
  telegram: { configured: boolean; paired: boolean }
  mcp: { name: string; connected: boolean; error: string | null }[]
  scheduler: { running: boolean; next: { id: number; at: string } | null }
}

// --- chat -------------------------------------------------------------------------------------------------

export type ConversationSource = 'web' | 'terminal' | 'telegram'

export interface Conversation {
  id: string
  source: ConversationSource
  started_at: string
  last_message_at: string | null
  preview: string
}

export interface Message {
  id: number
  role: 'user' | 'assistant'
  content: string
  created_at: string
}

export type ChatEvent =
  | { event: 'start'; data: { conversation_id: string; run_id: string } }
  | { event: 'delta'; data: { content: string; index: number } }
  | { event: 'tool'; data: { tool: string } }
  | { event: 'approval_required'; data: Approval }
  | { event: 'approval_resolved'; data: { approval_id: string; status: ApprovalStatus } }
  | { event: 'done'; data: { run_id: string; outcome: 'answered' | 'refused' | 'failed'; answer: string } }
  | { event: 'error'; data: { code: ErrorCode; message: string } }

// --- approvals --------------------------------------------------------------------------------------------

export type ApprovalStatus = 'pending' | 'approved' | 'denied' | 'expired' | 'preapproved'

export interface SuspiciousRecipient {
  value: string
  source: string
}

// The approval card: the same in the chat stream and the list
export interface Approval {
  approval_id: string
  tool: string
  arguments: Record<string, unknown>
  reason: string
  suspicious_recipients: SuspiciousRecipient[]
  untrusted_sources: string[]
  status: ApprovalStatus
  created_at: string
  expires_at: string | null
  channel: string
  run_id: string | null
}

export type Decision = 'approve' | 'deny'

// --- runs -------------------------------------------------------------------------------------------------

export type RunSource = 'chat' | 'telegram' | 'schedule'
export type RunOutcome = 'answered' | 'refused' | 'failed' | 'running'

export interface RunListItem {
  run_id: string
  started_at: string | null
  source: RunSource | null
  message: string
  outcome: RunOutcome
  turns: number
  tool_calls: number
  tokens: number
  stop_reason: string | null
  untrusted_sources: string[]
}

export interface RunDisclosure {
  mode: 'direct' | 'discovery' | null
  apps: 'find' | 'describe' | 'run' | null
  memory: 'L0-L1 prompt' | 'L2 search' | 'L3 read' | null
  skills_loaded: string[]
  web_pages_read: number
}

export interface RunSummary {
  source: RunSource | null
  message: string
  started_at: string | null
  outcome: RunOutcome
  duration_ms: number | null
  turns: number
  tokens: number
  tool_calls: number
  tool_errors: number
  denied: number
  stop_reason: string | null
  untrusted_sources: string[]
  reminders: string[]
  redactions: string[]
  tools_offered: number
  tool_schema_chars: number | null
  instructions_chars: number | null
  model: string | null
  model_upgrades: string[]
  disclosure: RunDisclosure
}

export interface RunEvent {
  seq: number
  type: string
  at: string | null
  payload: Record<string, unknown>
}

export interface RunDetail {
  run_id: string
  summary: RunSummary
  events: RunEvent[]
}

// --- memory -----------------------------------------------------------------------------------------------

export type Trust = 'high' | 'low'
export type NoteSource = 'user' | 'distill' | 'agent'

export interface NoteListItem {
  path: string
  title: string
  trust: Trust
  source: NoteSource
  updated_at: string
}

export interface Note extends NoteListItem {
  body: string
  source_detail: string
  links: string[]
  linked_from: string[]
}

export interface MemoryIndex {
  folders: Record<string, string>
  notes: NoteListItem[]
}

// --- skills -----------------------------------------------------------------------------------------------

export interface Skill {
  name: string
  description: string
  source: 'builtin' | 'user'
  requires: string[]
  model: string | null
  available: boolean
  missing: string[]
  files: string[]
  adjustments: string[]
}

export interface SkillDetail extends Skill {
  instructions: string
}

export interface SkillProposal {
  name: string
  description: string
  instructions: string
  requires: string[]
  proposed_by: string
}

// --- schedules --------------------------------------------------------------------------------------------

export interface Schedule {
  id: number
  message: string
  cron: string | null
  at: string | null
  timezone: string
  next_fire_at: string | null
  last_fired_at: string | null
  active: boolean
  when: string
}

// --- MCP and Telegram -------------------------------------------------------------------------------------

export interface McpServer {
  name: string
  transport: 'stdio' | 'http'
  target: string
  provides: string[]
  always_allow: string[]
  secrets: string[]
  connected: boolean
  error: string | null
}

export type ToolTier = 'read-only' | 'asks' | 'always allowed'

export interface McpTool {
  tool: string
  registered_name: string
  description: string
  tier: ToolTier
}

export interface Telegram {
  configured: boolean
  paired: boolean
  owner: string | null
}

export interface Pairing {
  code: string
  expires_at: string
}
