import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// The repo root: the mock backend reads the recorded runs in tests/core/runs/.
const repoRoot = fileURLToPath(new URL('..', import.meta.url))

// Relative base: the daemon serves the static build, from whatever path it chooses.
export default defineConfig({
  base: './',
  plugins: [react(), tailwindcss()],
  server: { fs: { allow: [repoRoot] } },
  build: { chunkSizeWarningLimit: 400 },
})
