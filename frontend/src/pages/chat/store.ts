import { useSyncExternalStore } from 'react'

import { api, errorMessage } from '../../api/client'
import type { Approval, ChatEvent, Message } from '../../api/types'
import { refreshPending } from '../../lib/pending'

// The open conversation, outside React: a streaming answer keeps going while the owner looks at another page.

export interface Turn {
  key: string
  user: string
  runId: string | null
  tools: string[]
  approvals: Approval[]
  text: string // the streamed deltas so far
  answer: string | null // the final text, from `done`
  outcome: 'answered' | 'refused' | 'failed' | null
  error: string | null
  running: boolean
}

export interface ChatState {
  conversationId: string | null
  history: Message[]
  loading: boolean
  loadError: string | null
  turns: Turn[]
  sending: boolean
}

let state: ChatState = { conversationId: null, history: [], loading: false, loadError: null, turns: [], sending: false }
const listeners = new Set<() => void>()
const conversationListeners = new Set<() => void>()

const set = (next: Partial<ChatState>) => {
  state = { ...state, ...next }
  listeners.forEach((listener) => listener())
}

const updateTurn = (key: string, change: (turn: Turn) => Partial<Turn>) =>
  set({ turns: state.turns.map((turn) => (turn.key === key ? { ...turn, ...change(turn) } : turn)) })

export const useChat = () =>
  useSyncExternalStore((listener) => {
    listeners.add(listener)
    return () => listeners.delete(listener)
  }, () => state)

// The conversation list reloads when a message starts or finishes one
export const onConversationsChanged = (listener: () => void) => {
  conversationListeners.add(listener)
  return () => {
    conversationListeners.delete(listener)
  }
}

export async function openConversation(id: string | null) {
  if (id === state.conversationId || state.sending) return
  set({ conversationId: id, history: [], turns: [], loadError: null, loading: Boolean(id) })
  if (!id) return
  try {
    const history = await api.messages(id)
    if (state.conversationId === id) set({ history, loading: false })
  } catch (e) {
    if (state.conversationId === id) set({ loadError: errorMessage(e), loading: false })
  }
}

// Puts a new conversation's id in the address without a page change
function showConversation(id: string) {
  history.replaceState(null, '', `#/chat/${encodeURIComponent(id)}`)
  window.dispatchEvent(new HashChangeEvent('hashchange'))
}

export function updateApproval(approval: Approval) {
  set({ turns: state.turns.map((turn) => ({ ...turn, approvals: turn.approvals.map((a) => (a.approval_id === approval.approval_id ? approval : a)) })) })
}

export async function send(message: string) {
  if (state.sending || !message.trim()) return
  const key = `t${Date.now()}`
  const turn: Turn = { key, user: message, runId: null, tools: [], approvals: [], text: '', answer: null, outcome: null, error: null, running: true }
  set({ sending: true, turns: [...state.turns, turn] })
  const deltas: string[] = []

  const onEvent = (event: ChatEvent) => {
    switch (event.event) {
      case 'start':
        updateTurn(key, () => ({ runId: event.data.run_id }))
        if (state.conversationId !== event.data.conversation_id) {
          set({ conversationId: event.data.conversation_id })
          showConversation(event.data.conversation_id)
        }
        conversationListeners.forEach((listener) => listener())
        break
      case 'delta':
        deltas[event.data.index] = event.data.content
        updateTurn(key, () => ({ text: deltas.join('') }))
        break
      case 'tool':
        updateTurn(key, (t) => ({ tools: [...t.tools, event.data.tool] }))
        break
      case 'approval_required':
        updateTurn(key, (t) => ({ approvals: [...t.approvals, event.data] }))
        void refreshPending()
        break
      case 'approval_resolved':
        updateTurn(key, (t) => ({ approvals: t.approvals.map((a) => (a.approval_id === event.data.approval_id ? { ...a, status: event.data.status } : a)) }))
        void refreshPending()
        break
      case 'done':
        updateTurn(key, () => ({ answer: event.data.answer, outcome: event.data.outcome, runId: event.data.run_id, running: false }))
        break
      case 'error':
        updateTurn(key, () => ({ error: event.data.message, running: false }))
        break
    }
  }

  try {
    await api.chat(message, state.conversationId, onEvent)
    updateTurn(key, (t) => (t.running ? { running: false, error: t.answer === null ? 'The answer stopped before it finished.' : null } : {}))
  } catch (e) {
    updateTurn(key, () => ({ error: errorMessage(e), running: false }))
  } finally {
    set({ sending: false })
    conversationListeners.forEach((listener) => listener())
  }
}
