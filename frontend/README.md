# Local Agent web UI

The page `local-agent daemon` serves on 127.0.0.1: chat, approvals, runs (the run inspector), memory, skills,
schedules and connected apps. Vite + React 19 + TypeScript + Tailwind 4. It talks to the local API described in
`docs/api.md` (contract v1).

```sh
npm install
npm run dev            # http://localhost:5173/?mock  (the mock backend, no daemon needed)
npm run build          # static files in dist/, relative paths
npm run lint
```

- **Against the daemon:** `npm run build -- --watch` and run the daemon with `LOCAL_AGENT_UI_DIR=frontend/dist`,
  then open the link it prints (`http://127.0.0.1:7878/#token=...`). Or use `npm run dev` with
  `[daemon] dev_origin = "http://localhost:5173"`, after opening the dev page once with `#token=...`.
- **The mock backend** (`src/api/mock/`) answers like the daemon, from memory: the recorded runs in
  `tests/core/runs/*.json`, sample notes, skills, schedules and servers, and scripted chat replies ("summarize my
  inbox" asks to email a stranger, "remind me every Friday..." asks to create a schedule). Turn it on with `?mock`
  in the address or `VITE_LOCAL_AGENT_MOCK=1` at build time. It's a separate chunk that a page served by the
  daemon never loads.
- **Routes are in the fragment** (`#/runs/<id>`): the build uses relative asset paths, so the page stays at the
  root URL. The `#token=` fragment is read once, kept in `localStorage` (`local-agent-token`), and removed from the
  address bar.
- **Fonts are bundled** (Fontsource, Latin subsets): the page makes no request off the machine.
