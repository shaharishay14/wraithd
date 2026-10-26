# The web

**Files:** `tools/web.py`, `tools/__init__.py` (which search), `core/egress.py`

## One-liner

> `read_website` opens one page, in parts, through the egress guard, so nothing on the laptop or its network can
> be reached. `web_search` returns snippets from the model provider's own search, on the key the owner already
> set (DuckDuckGo for local models, SerpAPI as an option). Everything they return is third-party content.

## How it works

- **`read_website(url, offset)`:** fetches through `egress.safe_get`, reduces HTML to its visible text (one line
  per block, no scripts or styles), and returns 3,500 characters per call with `next_offset` for the next part.
- **The egress guard, on every hop:** http(s) on default ports only, no `user:pass@`, every address the host
  resolves to must be public, redirects are followed by hand and each new address is checked, and the connected
  peer is checked again before the body is read. 2 MB cap.
- **`web_search(query, num_results)`:** goes through a small `SearchProvider` interface, one class per backend.
  `[web] search` picks it (`tools/__init__.py`, `choose_search`):

  | `search =` | Backend | Key | Cost to the owner |
  |---|---|---|---|
  | `"auto"` (default) | the model provider's own: `openai` or `anthropic`; `duckduckgo` for ollama and openai-compatible | the model key | see the rows below |
  | `"openai"` | Responses API, hosted `web_search` tool, on `gpt-4o-mini` | `openai` | about $10 per 1,000 searches, plus the small model's tokens |
  | `"anthropic"` | Messages API, server tool `web_search_20250305` (`max_uses` 1), on `claude-haiku-5-5` | `anthropic` | about $10 per 1,000 searches, plus the small model's tokens |
  | `"duckduckgo"` | the HTML page (`html.duckduckgo.com/html`), parsed with the standard library, through the egress guard | none | free; it may rate-limit or ask for a captcha |
  | `"serpapi"` | Google results through serpapi.com, as in agent-studio | `serpapi` | the SerpAPI plan |
  | `""` | off | | |

  When the backend's key is missing, the tool isn't offered, and neither are the skills that need it
  (research-report); `local-agent doctor` says which backend is active and why. Errors never include the request
  URL or a key.
- **Ours, not the provider's tool:** the native backends run the provider's hosted search inside one small model
  call of their own and turn its result into rows: OpenAI's url citations (with the sentence each one closes),
  Anthropic's `web_search_tool_result` blocks (with the text the answer cited as the snippet). The agent's model
  never gets the hosted tool, so the permission gate, the event log and the third-party wrapping see every search,
  and the agent's model doesn't have to be one that supports it.
- **A small model, not the configured one:** the search call only has to run one search and list what it found,
  so it uses the provider's cheapest model (`gpt-4o-mini`, `claude-haiku-5-5` at low effort) whatever the run's
  model is. The search fee is the same either way; the tokens are a fraction.
- **Anthropic organizations** may have to turn web search on in the Claude Console first. Until then the API
  refuses it, and the tool says exactly that instead of a generic failure.
- **What it cost:** budgets are tokens, not dollars (`core/stop.py`). The tool's result carries `usage` (`backend`,
  `searches`, `input_tokens`, `output_tokens`); the gate copies it into the `tool_result` event, and
  `views.tokens_used` adds those tokens to the run's, so native searches count toward the run and daily token
  budgets. The per-search fee isn't in any budget: it shows up only on the provider's bill.
- **DuckDuckGo result links** are redirects (`//duckduckgo.com/l/?uddg=<url>`): they're unwrapped to the real URL,
  and its ads (`duckduckgo.com/y.js`) are dropped. Like `read_website`, the request goes through `egress.safe_get`,
  so a poisoned DNS answer can't turn it toward the laptop or its network.
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
