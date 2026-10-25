import { useSyncExternalStore } from 'react'

// Hash routes (#/runs/<id>): the build uses relative asset paths, so the page must stay at the daemon's root URL
// for ./assets/ to resolve. The #token=... fragment the daemon prints isn't a route; config.ts removes it first.

const subscribe = (callback: () => void) => {
  window.addEventListener('hashchange', callback)
  return () => window.removeEventListener('hashchange', callback)
}

const current = () => (location.hash.startsWith('#/') ? location.hash.slice(1) : '/')

export function useRoute(): string[] {
  const path = useSyncExternalStore(subscribe, current)
  return path.split('/').filter(Boolean).map((part) => {
    try {
      return decodeURIComponent(part)
    } catch {
      return part
    }
  })
}

export const href = (...parts: string[]) => '#/' + parts.map(encodeURIComponent).join('/')

export const navigate = (...parts: string[]) => {
  location.hash = href(...parts).slice(1)
}
