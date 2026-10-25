import { useCallback, useEffect, useRef, useState } from 'react'

import { errorMessage } from '../api/client'

// Loads data once (and again on reload(), or every `pollMs` while the tab is visible). Keeps showing the last
// data while it reloads, so polling never flashes a spinner.
export function useResource<T>(load: () => Promise<T>, deps: unknown[], pollMs?: number) {
  const [data, setData] = useState<T | undefined>(undefined)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const loadRef = useRef(load)
  const generation = useRef(0)

  useEffect(() => {
    loadRef.current = load
  })

  const reload = useCallback(async () => {
    const mine = ++generation.current
    try {
      const value = await loadRef.current()
      if (mine !== generation.current) return
      setData(value)
      setError(null)
    } catch (e) {
      if (mine !== generation.current) return
      setError(errorMessage(e))
    } finally {
      if (mine === generation.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    setLoading(true)
    setData(undefined)
    void reload()
    if (!pollMs) return
    const timer = setInterval(() => {
      if (document.visibilityState === 'visible') void reload()
    }, pollMs)
    return () => clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  return { data, error, loading: loading && data === undefined, reload, setData }
}

// Ticks every `ms` so relative times ("in 42 s") stay current
export function useNow(ms = 1000) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), ms)
    return () => clearInterval(timer)
  }, [ms])
  return now
}
