import { useSyncExternalStore } from 'react'

import { api } from '../api/client'
import type { Approval } from '../api/types'

// Pending approvals, shared by the nav badge, the approvals page and the chat: polled every few seconds while
// the tab is visible, and refreshed at once after any decision.

let pending: Approval[] = []
const listeners = new Set<() => void>()
let timer: ReturnType<typeof setInterval> | null = null

async function refresh() {
  try {
    pending = await api.approvals('pending')
    listeners.forEach((listener) => listener())
  } catch {
    // The page that shows them reports errors; the badge just keeps its last count
  }
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  if (!timer) {
    void refresh()
    timer = setInterval(() => document.visibilityState === 'visible' && void refresh(), 3000)
  }
  return () => {
    listeners.delete(listener)
    if (!listeners.size && timer) {
      clearInterval(timer)
      timer = null
    }
  }
}

export const usePendingApprovals = () => useSyncExternalStore(subscribe, () => pending)
export const refreshPending = refresh
