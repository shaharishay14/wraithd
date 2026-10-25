// Where the API is and how to authenticate (docs/api.md, "Auth: the token").
//
// The daemon prints http://127.0.0.1:7878/#token=<token>. The token moves from the fragment to localStorage and
// leaves the address bar, so it isn't left in history, bookmarks or screenshots. The API is always /api on the
// page's own origin: the daemon serves the UI and the API together.

export const TOKEN_KEY = 'local-agent-token'
const MOCK_KEY = 'local-agent-mock'

const storage = {
  get(key: string) {
    try {
      return localStorage.getItem(key)
    } catch {
      return null
    }
  },
  set(key: string, value: string) {
    try {
      localStorage.setItem(key, value)
    } catch {
      // Private mode or blocked storage: the token lasts for this page only
    }
  },
}

let memoryToken: string | null = null

// Reads #token=... once at startup, keeps it, and strips it from the address bar
export function takeTokenFromHash() {
  const match = /^#token=([^&]+)/.exec(location.hash)
  if (!match) return
  memoryToken = decodeURIComponent(match[1])
  storage.set(TOKEN_KEY, memoryToken)
  history.replaceState(null, '', location.pathname + location.search)
}

export const getToken = () => memoryToken ?? storage.get(TOKEN_KEY)

export const API_BASE = '/api'

// The mock backend (src/api/mock/) answers instead of a daemon: for development, design checks and demos.
// Turn it on with ?mock in the address (remembered for the tab) or VITE_LOCAL_AGENT_MOCK=1 at build time.
export function mockBackendEnabled() {
  if (import.meta.env.VITE_LOCAL_AGENT_MOCK === '1') return true
  try {
    if (new URLSearchParams(location.search).has('mock')) sessionStorage.setItem(MOCK_KEY, '1')
    return sessionStorage.getItem(MOCK_KEY) === '1'
  } catch {
    return new URLSearchParams(location.search).has('mock')
  }
}
