// The typed client for the local API (docs/api.md). Every call goes through one transport: window.fetch against
// the daemon, or the mock backend's fetch, so the mock exercises exactly this code.

import { API_BASE, getToken } from '../config'
import { readSse } from './sse'
import type {
  Approval, ApprovalStatus, ApiErrorBody, ChatEvent, Conversation, Decision, ErrorCode, McpServer, McpTool,
  MemoryIndex, Message, Note, Pairing, RunDetail, RunListItem, Schedule, Skill, SkillDetail, SkillProposal, Status,
  Telegram,
} from './types'

export type Transport = (input: string, init: RequestInit) => Promise<Response>

export class ApiError extends Error {
  readonly status: number
  readonly code: ErrorCode | 'network'

  constructor(status: number, code: ErrorCode | 'network', message: string) {
    super(message)
    this.status = status
    this.code = code
  }
}

let transport: Transport = (input, init) => fetch(input, init)
const unauthorizedListeners = new Set<() => void>()

export const setTransport = (next: Transport) => {
  transport = next
}

// The app shows "open the link printed by local-agent daemon" on any 401
export const onUnauthorized = (listener: () => void) => {
  unauthorizedListeners.add(listener)
  return () => {
    unauthorizedListeners.delete(listener)
  }
}

async function send(method: string, path: string, body?: unknown, signal?: AbortSignal): Promise<Response> {
  const headers: Record<string, string> = { Accept: 'application/json' }
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`
  if (body !== undefined) headers['Content-Type'] = 'application/json'

  let response: Response
  try {
    response = await transport(API_BASE + path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
      credentials: 'omit',
      cache: 'no-store',
    })
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') throw e
    throw new ApiError(0, 'network', 'Local Agent is not answering. Is `local-agent daemon` running?')
  }

  if (response.ok) return response
  let code: ErrorCode = 'internal'
  let message = `The daemon answered ${response.status}.`
  try {
    const parsed = (await response.json()) as ApiErrorBody
    code = parsed.error.code
    message = parsed.error.message
  } catch {
    // Not the contract's error shape (a proxy, a crash page): keep the generic message
  }
  if (response.status === 401) unauthorizedListeners.forEach((listener) => listener())
  throw new ApiError(response.status, code, message)
}

async function json<T>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await send(method, path, body)
  return (response.status === 204 ? undefined : await response.json()) as T
}

const seg = encodeURIComponent
// people/dana.md -> /people/dana.md, each part encoded
const notePath = (path: string) => path.split('/').map(seg).join('/')

// One message to the agent. Calls onEvent for each event in the stream, in order, and resolves after done or
// error. Throws ApiError if the request itself is refused (409 when the conversation is busy).
async function chat(message: string, conversationId: string | null, onEvent: (event: ChatEvent) => void, signal?: AbortSignal) {
  const response = await send('POST', '/chat', conversationId ? { message, conversation_id: conversationId } : { message }, signal)
  if (!response.body) throw new ApiError(0, 'internal', 'The chat answer had no body.')
  for await (const { event, data } of readSse(response.body)) {
    let parsed: unknown
    try {
      parsed = JSON.parse(data)
    } catch {
      continue
    }
    onEvent({ event, data: parsed } as ChatEvent)
    if (event === 'done' || event === 'error') return
  }
}

export const api = {
  health: () => json<{ ok: boolean; version: string }>('GET', '/health'),
  status: () => json<Status>('GET', '/status'),

  chat,
  conversations: () => json<Conversation[]>('GET', '/conversations'),
  messages: (conversationId: string) => json<Message[]>('GET', `/conversations/${seg(conversationId)}/messages`),

  // One status, several, or 'all'
  approvals: (status: ApprovalStatus | ApprovalStatus[] | 'all' = 'pending') =>
    json<Approval[]>('GET', `/approvals?status=${[status].flat().join(',')}`),
  decide: (approvalId: string, decision: Decision) => json<Approval>('POST', `/approvals/${seg(approvalId)}`, { decision }),

  runs: (limit = 50) => json<RunListItem[]>('GET', `/runs?limit=${limit}`),
  run: (runId: string) => json<RunDetail>('GET', `/runs/${seg(runId)}`),

  memory: () => json<MemoryIndex>('GET', '/memory'),
  note: (path: string) => json<Note>('GET', `/memory/${notePath(path)}`),
  saveNote: (path: string, body: string, title?: string) => json<Note>('PUT', `/memory/${notePath(path)}`, title ? { body, title } : { body }),
  deleteNote: (path: string) => json<void>('DELETE', `/memory/${notePath(path)}`),

  skills: () => json<Skill[]>('GET', '/skills'),
  skill: (name: string) => json<SkillDetail>('GET', `/skills/${seg(name)}`),
  setAdjustments: (name: string, rules: string[]) => json<Skill>('PUT', `/skills/${seg(name)}/adjustments`, { rules }),
  proposals: () => json<SkillProposal[]>('GET', '/skills/proposals'),
  approveProposal: (name: string) => json<Skill>('POST', `/skills/proposals/${seg(name)}/approve`),
  deleteProposal: (name: string) => json<void>('DELETE', `/skills/proposals/${seg(name)}`),

  schedules: (all = false) => json<Schedule[]>('GET', `/schedules?all=${all}`),
  cancelSchedule: (id: number) => json<void>('DELETE', `/schedules/${id}`),

  mcpServers: () => json<McpServer[]>('GET', '/mcp/servers'),
  mcpTools: (server: string) => json<McpTool[]>('GET', `/mcp/servers/${seg(server)}/tools`),
  alwaysAllow: (server: string, tool: string) => json<McpServer>('PUT', `/mcp/servers/${seg(server)}/always-allow/${seg(tool)}`),
  askAgain: (server: string, tool: string) => json<McpServer>('DELETE', `/mcp/servers/${seg(server)}/always-allow/${seg(tool)}`),

  telegram: () => json<Telegram>('GET', '/telegram'),
  pair: () => json<Pairing>('POST', '/telegram/pairing'),
}

export const errorMessage = (e: unknown) => (e instanceof ApiError ? e.message : 'Something went wrong.')
