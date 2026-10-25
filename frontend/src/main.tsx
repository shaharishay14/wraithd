import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import './styles/fonts.css'
import './styles/index.css'

import { setTransport } from './api/client'
import App from './App'
import { takeTokenFromHash, mockBackendEnabled } from './config'

async function start() {
  takeTokenFromHash()
  // A fresh link from `local-agent daemon` pasted into this tab only changes the fragment: take it and start over
  window.addEventListener('hashchange', () => {
    if (location.hash.startsWith('#token=')) {
      takeTokenFromHash()
      location.reload()
    }
  })
  const mock = mockBackendEnabled()
  if (mock) {
    // Its own chunk: a build served by the daemon never loads it
    const { mockFetch } = await import('./api/mock/server')
    setTransport(mockFetch)
  }
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <App mock={mock} />
    </StrictMode>,
  )
}

void start()
