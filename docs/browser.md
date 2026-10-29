# The browser

**Files:** `tools/browser.py`, the capabilities blocks in `core/gate.py`, `config.py` (`[browser]`) and `run.py`

## One-liner

> A headless Chromium the agent can open pages in, read, and act on, with every request the page makes passing
> the egress guard. Reading is free; clicking, typing and submitting are writes the owner approves, and never
> toward a site that came from someone else's text.

## How it works

- **On:** `[browser] enabled = true`, plus the optional extra: `uv tool install 'local-agent[browser]'` and
  `playwright install chromium`. Without either, the tools aren't offered and nothing imports Playwright. Only the
  owner's runs get it.
- **Tools.** `browser_open(url)`, `browser_read()`, `browser_screenshot()` are reads. `browser_click(ref, to_site)`,
  `browser_type(ref, text, to_site)`, `browser_submit(ref, to_site)` are writes. A read returns the page text (8,000
  characters) and numbered elements (`[3] button 'Pay'`); an action takes that number.
- **`to_site`** is the host an action goes to: where a link points, where a form posts, or the page itself (it sees
  what's typed). The tool refuses unless it's the real target, so the model can't name the owner's shop and post the
  form somewhere else. The gate treats it as a recipient ([security](security.md)): after a page is read (always,
  since reading taints), a pre-approved action only runs toward a site the owner wrote. In a conversation the card
  names a site the owner never wrote and which page it came from; unattended it's refused.
- **Every request, not just navigations.** Chromium starts with `--host-resolver-rules=MAP * ~NOTFOUND`: it can't
  resolve a single host itself. Every request is routed to `EgressRouter`, which fetches GETs with `egress.safe_get`
  (every hop and the connected peer checked) and other methods with the same URL and peer checks, without
  following redirects (the browser asks for the new location, which is checked again). So a page script fetching
  `http://127.0.0.1:7878/api/...`, the router or `169.254.169.254` gets an aborted request. WebSockets are closed,
  service workers blocked, WebRTC limited to proxied UDP (none). Cookies and credentials headers never leave.
  Blocked requests are listed in the tool result.
- **The link rule.** `browser_open` follows `read_website`'s: after the run read private data, only links the owner
  wrote open freely (`URL_FETCH_TOOLS`).
- **Taint.** Every browser result is third-party content: wrapped, and it taints the run.
- **A fresh profile per run.** Each run gets its own browser context: no cookies, no logins, nothing kept. It closes
  when the run ends, or after `idle_seconds` (60) without an action.
- **Screenshots** are saved to `workspace/files/browser/` for the owner, and the model gets the page text. Sending
  the image to a model with vision needs the gate to pass image results through, which it doesn't yet.

## Tests

The Playwright part sits behind `BrowserDriver`. `JsonSiteDriver` is a browser without Chromium: pages are JSON
documents (`title`, `text`, `elements`, and `requests` standing for what the page's scripts fetch), fetched
through the same router and egress checks from an `httpx.MockTransport`. Set `rt.browser_driver = JsonSiteDriver`
and `rt.web_transport`. The test-only egress override lives in `tests/capabilities/fakes.py`: `*.example` hosts
resolve to a public address, everything else goes through the real check. The real-Chromium test runs with
`LOCAL_AGENT_REAL_BROWSER=1`.

## On a laptop

A browser on a server reaches the server's network; on a laptop it reaches everything that trusts the laptop, and
it's the one tool whose pages run their own code. So the rule that keeps `read_website` out of localhost and the LAN
has to hold for the page's scripts too, which is why the browser resolves nothing itself.

## What's honest to say

- The non-GET peer check happens after the request is sent, as for `read_website`.
- There is no pre-approval for browser writes today (pre-approval is per MCP tool), so they always ask, and are
  refused unattended. The `to_site` rule is what holds when there is one.
- Chromium may still try speculative connections (preconnect) to a page's hosts; with no resolver they fail.
