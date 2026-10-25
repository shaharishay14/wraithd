import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import { ArrowUp, ArrowUpRight, Plus, Wrench } from 'lucide-react'

import { api } from '../../api/client'
import type { Conversation, Message } from '../../api/types'
import { cn } from '../../lib/cn'
import { formatWhen } from '../../lib/format'
import { href, navigate } from '../../lib/router'
import { useResource } from '../../lib/useResource'
import { ApprovalCard } from '../../ui/ApprovalCard'
import { buttonClass } from '../../ui/buttonClass'
import { Spinner } from '../../ui/Spinner'
import { onConversationsChanged, openConversation, send, updateApproval, useChat, type Turn } from './store'

const SOURCE: Record<Conversation['source'], string> = { web: 'Web', terminal: 'Terminal', telegram: 'Telegram' }

const SUGGESTIONS = ['Summarize my inbox', 'Remind me every Friday to send the invoice', 'Remember that I prefer short replies']

function Bubble({ role, children }: { role: 'user' | 'assistant'; children: React.ReactNode }) {
  return role === 'user' ? (
    <div className="ms-auto max-w-[85%] rounded-[20px] rounded-br-md bg-white px-4 py-2.5 break-words whitespace-pre-wrap text-on-white sm:max-w-[75%]">{children}</div>
  ) : (
    <div className="me-auto max-w-[92%] rounded-[20px] rounded-bl-md border border-white/12 bg-night-3 px-4 py-2.5 break-words whitespace-pre-wrap text-white/95 sm:max-w-[80%]">{children}</div>
  )
}

function RunLink({ runId, label = 'See what it did' }: { runId: string; label?: string }) {
  return (
    <a href={href('runs', runId)} className="me-auto inline-flex items-center gap-1 ps-1 text-xs text-white/50 no-underline hover:text-white">
      {label}
      <ArrowUpRight className="size-3" strokeWidth={1.7} aria-hidden="true" />
    </a>
  )
}

function HistoryMessage({ message }: { message: Message }) {
  return (
    <>
      <Bubble role={message.role}>{message.content}</Bubble>
      {message.role === 'assistant' && message.run_id && <RunLink runId={message.run_id} />}
    </>
  )
}

function LiveTurn({ turn }: { turn: Turn }) {
  const text = turn.answer ?? turn.text
  const waitingOnYou = turn.approvals.some((a) => a.status === 'pending')
  return (
    <>
      <Bubble role="user">{turn.user}</Bubble>
      {(turn.tools.length > 0 || (turn.running && !text)) && (
        <div className="me-auto flex max-w-full flex-col gap-1.5 ps-1 text-[13px] text-white/65" aria-live="polite">
          {turn.running && !text && (
            <span className="flex items-center gap-2 text-white/80">
              <Spinner size={14} />
              {waitingOnYou ? 'Waiting for your approval' : 'Working on it'}
            </span>
          )}
          {turn.tools.map((tool, i) => (
            <span key={i} className="flex items-center gap-2">
              <Wrench className="size-3.5 shrink-0 text-white/45" strokeWidth={1.7} aria-hidden="true" />
              <span className="font-mono break-words">{tool}</span>
            </span>
          ))}
        </div>
      )}
      {turn.approvals.map((approval) => (
        <div key={approval.approval_id} className="me-auto w-full max-w-[560px]">
          <ApprovalCard approval={approval} onChange={updateApproval} compact />
        </div>
      ))}
      {text && (
        <Bubble role="assistant">
          {text}
          {turn.running && <span className="ms-0.5 inline-block h-4 w-1.5 animate-pulse rounded-sm bg-white/60 align-[-2px]" aria-hidden="true" />}
        </Bubble>
      )}
      {turn.error && (
        <p className="m-0 me-auto rounded-xl border border-off/40 bg-off/8 px-3 py-2 text-sm text-white" role="alert">
          {turn.error}
        </p>
      )}
      {!turn.running && turn.runId && (
        <RunLink runId={turn.runId} label={`${turn.outcome && turn.outcome !== 'answered' ? `${turn.outcome} · ` : ''}See what it did`} />
      )}
    </>
  )
}

function Composer({ disabled }: { disabled: boolean }) {
  const [text, setText] = useState('')
  const ref = useRef<HTMLTextAreaElement>(null)

  const submit = (e?: FormEvent) => {
    e?.preventDefault()
    if (!text.trim() || disabled) return
    void send(text.trim())
    setText('')
  }
  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      submit()
    }
  }
  useEffect(() => {
    const el = ref.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 180)}px`
  }, [text])

  return (
    <form onSubmit={submit} className="flex items-end gap-2 rounded-[26px] border border-white/22 bg-night-2 p-2 ps-4 shadow-[0_0_40px_-12px_rgba(127,166,196,0.35)] focus-within:border-white/50">
      <label htmlFor="composer" className="sr-only">Message your agent</label>
      <textarea
        id="composer"
        ref={ref}
        rows={1}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={onKeyDown}
        placeholder="Ask your agent…"
        className="max-h-[180px] min-h-9 flex-1 resize-none bg-transparent py-1.5 text-[15px] leading-6 text-white placeholder:text-white/45 focus:outline-none"
      />
      <button
        type="submit"
        disabled={disabled || !text.trim()}
        aria-label="Send"
        className="grid size-9 shrink-0 place-items-center rounded-full bg-white text-on-white transition-opacity disabled:opacity-35 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white"
      >
        <ArrowUp className="size-4" strokeWidth={2} aria-hidden="true" />
      </button>
    </form>
  )
}

function ConversationList({ conversations, current }: { conversations: Conversation[]; current: string | null }) {
  return (
    <nav aria-label="Conversations" className="flex min-h-0 flex-col gap-2">
      <a href={href('chat')} className={buttonClass('glass', 'sm', 'justify-start')}>
        <Plus />
        New conversation
      </a>
      <ul className="m-0 flex min-h-0 list-none flex-col gap-0.5 overflow-y-auto p-0">
        {conversations.map((c) => (
          <li key={c.id}>
            <a
              href={href('chat', c.id)}
              aria-current={c.id === current ? 'page' : undefined}
              className={cn('flex flex-col rounded-xl px-3 py-2 no-underline', c.id === current ? 'bg-white/10' : 'hover:bg-white/5')}
            >
              <span className="truncate text-sm text-white/90">{c.preview || 'New conversation'}</span>
              <span className="text-xs text-white/50">{SOURCE[c.source]} · {formatWhen(c.last_message_at ?? c.started_at)}</span>
            </a>
          </li>
        ))}
      </ul>
    </nav>
  )
}

export default function Chat({ conversationId }: { conversationId: string | null }) {
  const chat = useChat()
  const conversations = useResource(() => api.conversations(), [])
  const endRef = useRef<HTMLDivElement>(null)
  const current = conversations.data?.find((c) => c.id === chat.conversationId)
  const readOnly = current ? current.source !== 'web' : false
  const empty = !chat.loading && chat.history.length === 0 && chat.turns.length === 0

  useEffect(() => {
    void openConversation(conversationId)
  }, [conversationId])
  const reloadConversations = conversations.reload
  useEffect(() => onConversationsChanged(() => void reloadConversations()), [reloadConversations])

  const last = chat.turns[chat.turns.length - 1]
  useEffect(() => {
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    endRef.current?.scrollIntoView({ block: 'end', behavior: reduce ? 'auto' : 'smooth' })
  }, [chat.history.length, chat.turns.length, last?.text, last?.approvals.length, last?.tools.length])

  return (
    <div className="flex h-[calc(100dvh-94px)] md:h-dvh">
      <aside className="hidden w-[260px] shrink-0 flex-col border-e border-white/8 p-3 lg:flex">
        <ConversationList conversations={conversations.data ?? []} current={chat.conversationId} />
      </aside>

      <section className="flex min-w-0 flex-1 flex-col" aria-label="Conversation">
        <div className="flex items-center gap-2 border-b border-white/8 px-4 py-2.5 lg:hidden">
          <label htmlFor="conversation-picker" className="sr-only">Conversation</label>
          <select
            id="conversation-picker"
            value={chat.conversationId ?? ''}
            onChange={(e) => (e.target.value ? navigate('chat', e.target.value) : navigate('chat'))}
            disabled={chat.sending}
            className="h-8 min-w-0 flex-1 rounded-pill border border-white/16 bg-night-2 px-3 text-sm text-white"
          >
            <option value="">New conversation</option>
            {(conversations.data ?? []).map((c) => (
              <option key={c.id} value={c.id}>{SOURCE[c.source]}: {c.preview || 'New conversation'}</option>
            ))}
          </select>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto flex max-w-[780px] flex-col gap-3 px-4 py-6 sm:px-6">
            {chat.loading && <div className="grid place-items-center py-16"><Spinner label="Loading the conversation" /></div>}
            {chat.loadError && <p className="m-0 text-sm text-off" role="alert">{chat.loadError}</p>}
            {empty && !chat.loadError && (
              <div className="flex flex-col items-center gap-5 py-[12vh] text-center">
                <h1 className="m-0 font-display text-[30px] leading-9 font-medium tracking-[-0.03em] sm:text-[36px] sm:leading-11">
                  What can I <em className="sky-serif">do for you?</em>
                </h1>
                <p className="m-0 max-w-[46ch] text-white/65">Anything that writes, sends or schedules asks you first, here or in Approvals.</p>
                <div className="flex flex-wrap justify-center gap-2">
                  {SUGGESTIONS.map((s) => (
                    <button key={s} type="button" onClick={() => void send(s)} className={buttonClass('glass', 'sm', 'font-medium')}>
                      {s}
                    </button>
                  ))}
                </div>
              </div>
            )}
            {chat.history.map((m) => <HistoryMessage key={m.id} message={m} />)}
            {chat.turns.map((turn) => <LiveTurn key={turn.key} turn={turn} />)}
            <div ref={endRef} />
          </div>
        </div>

        <div className="mx-auto w-full max-w-[780px] px-4 pt-1 pb-4 sm:px-6">
          {readOnly ? (
            <p className="m-0 rounded-2xl border border-white/12 bg-night-2 px-4 py-3 text-sm text-white/65">
              This conversation happened in {SOURCE[current!.source]}, so it's read-only here. <a href={href('chat')} className="text-white underline">Start a new one</a>.
            </p>
          ) : (
            <Composer disabled={chat.sending} />
          )}
        </div>
      </section>
    </div>
  )
}
