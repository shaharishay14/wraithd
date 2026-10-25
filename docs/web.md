# The web

**Files:** `tools/web.py`, `core/egress.py`

## One-liner

> `read_website` opens one page, in parts, through the egress guard, so nothing on the laptop or its network can
> be reached. `web_search` returns snippets from a search provider, and exists only once the owner has set its
> key. Everything they return is third-party content.

## How it works

- **`read_website(url, offset)`:** fetches through `egress.safe_get`, reduces HTML to its visible text (one line
  per block, no scripts or styles), and returns 3,500 characters per call with `next_offset` for the next part.
- **The egress guard, on every hop:** http(s) on default ports only, no `user:pass@`, every address the host
  resolves to must be public, redirects are followed by hand and each new address is checked, and the connected
  peer is checked again before the body is read. 2 MB cap.
- **`web_search(query, num_results)`:** goes through a small `SearchProvider` interface. SerpAPI is the first
  provider, as in agent-studio. The key is set with `local-agent key set serpapi` (or `SERPAPI_KEY`); without one
  the tool isn't offered, and neither are the skills that need it (research-report). Errors never include the
  request URL, since the key travels in it.
- **Taint:** pages and snippets are written by strangers. Both tools' output is wrapped as third-party content, the
  run is tainted from then on (recipients must come from the owner), and the first result gets the
  `untrusted_content` reminder.
- **Links after private data:** once the run has read the owner's memory or files, `read_website` opens freely only
  links the owner wrote; any other link needs approval, so a page can't make the agent leak data through URLs.

## On a laptop

On EC2 the thing to protect was the metadata service at `169.254.169.254`. On a laptop it's everything that
trusts the machine: localhost (dev servers, Ollama on port 11434, the agent's own API in Phase 5), the home
router, printers, a NAS, `.local` names. The same rule ("public addresses only, on every hop, after DNS")
blocks all of them, and the tests try each way in: a loopback or LAN address directly, a hostname that resolves
to `127.0.0.1` or `192.168.x`, an mDNS name, and a public page that redirects inward.

Two changes from agent-studio's port:
- HTML is reduced to text with the standard library's parser instead of BeautifulSoup, so there's one dependency
  less.
- Fetches use `httpx` (the plan's stack) instead of `requests`, run in a thread so they don't block the loop.
