# telos-cardea

Telos's routine tools packaged as [Cardea](https://github.com/pasinit/cardea) installed operations
(ADR-023, ADR-026, ADR-027). A Cardea agent run exposes only installed operations to its Claude session,
so every tool call gets an intent, a receipt and, for effects, an idempotency scope.

The package is self-contained: it never imports the `telos` app and adds nothing to Telos's own dependencies.
Cardea installs it into its service environment on its own host.

## Operations

| Operation | Telos tool | Effect | Idempotency scope | Reconciler | Profile |
|---|---|---|---|---|---|
| `mail.send` | `send_email` | effect | `["/to"]` (one email per recipient per idempotency context) | `mail.send.reconcile` | in-process |
| `mail.preview` | (shadow mode) | read | none | none | in-process |
| `listings.rightmove_search` | `rightmove_search` | read | none | none | in-process (browser fallback: confined) |
| `listings.zoopla_search` | `zoopla_search` | read | none | none | in-process (browser fallback: confined) |
| `listings.agency_search` | `agency_search` | read | none | none | in-process (browser fallback: confined) |
| `web.search` | `web_search` | read | none | none | in-process |
| `browser.open` | `browser_open` | read | none | none | **confined worker** |
| `browser.click` | `browser_click` | read | none | none | **confined worker** |
| `memory.recall` | `memory_recall` | read | none | none | in-process |

Descriptors are in `descriptors/`, one JSON file per operation: an MCP `Tool` with input and output schemas,
plus the `cardea` binding. Handlers are registered under the `cardea.operations` entry-point group with the
same names.

### Confined-worker profile

`browser.open` and `browser.click` launch a headless Chromium (patchright) **per call**. Telos's browser tools
caused OOM kills on the 1.9 GB host, so do not install or grant these operations in Cardea's in-process
profile. Install them only where Cardea's configurable confined workers (ADR-022 gap #6) can bound their
memory. They need the `browser` extra and `patchright install chromium`.

The listing operations do not start a browser by default. They fetch with `curl_cffi`, then use the
unblocker proxy if one is configured. A browser render runs only when `browser_fallback` is enabled, which
also needs the confined profile. Without it, agencies whose cards are filled in client-side (Dexters prices)
use their server HTML, and anti-bot walls report `status: "unavailable"`.

## Install

On the Cardea host, into Cardea's service environment:

```sh
pip install "telos-cardea @ git+https://github.com/pasinit/telos.git@<sha>#subdirectory=packages/telos-cardea"
# optional extras: [search] free DDGS search, [browser] confined-worker profile only
```

Copy the descriptors you want to install into Cardea's `operations_dir`, owned by the service account and
private:

```sh
install -m 0600 -o cardea packages/telos-cardea/descriptors/{mail.send,web.search,memory.recall}.json /etc/cardea/operations/
```

Cardea resolves each descriptor's `handler` and `reconciler` from the installed entry points at start-up.

## Configuration

Credentials never come from operation arguments. Each call reads environment variables, then the private
TOML file named by `TELOS_CARDEA_CONFIG` (default `/etc/telos-cardea/config.toml`). Environment variables
win. The file must be mode `0600`, or the call fails. Environment names match Telos's, so one `.env` can serve both.

| Setting | Env var | File key | Used by |
|---|---|---|---|
| SMTP relay host (Brevo: `smtp-relay.brevo.com`) | `SMTP_HOST` | `smtp.host` | `mail.send` |
| SMTP port (default 587, STARTTLS) | `SMTP_PORT` | `smtp.port` | `mail.send` |
| SMTP login / password | `SMTP_USER` / `SMTP_PASSWORD` | `smtp.user` / `smtp.password` | `mail.send` |
| Sender (also the Message-ID domain) | `SMTP_FROM` | `smtp.from` | `mail.send`, reconciler |
| Brevo API key (transactional events) | `BREVO_API_KEY` | `brevo.api_key` | `mail.send.reconcile` |
| Recordatio base URL | `RECORDATIO_URL` | `recordatio.url` | `memory.recall` |
| Recordatio bearer key | `CHAT_API_KEY` | `recordatio.api_key` | `memory.recall` |
| Cardea namespace → Recordatio user/agent | none | `recordatio.scopes."<namespace>"` | `memory.recall` |
| Search engine: `serper`, `serpapi`, `duckduckgo` (default), `bing` | `SEARCH_ENGINE` | `search.engine` | `web.search` |
| Serper / SerpAPI keys | `SERPER_KEY` / `SERPAPI_KEY` | `search.serper_key` / `search.serpapi_key` | `web.search` |
| Unblocker URL template with `{url}` | `TELOS_UNBLOCKER_URL` | `fetch.unblocker_url` | listings |
| Browser fallback for listings | `TELOS_CARDEA_BROWSER_FALLBACK` | `fetch.browser_fallback` | listings |

```toml
[smtp]
host = "smtp-relay.brevo.com"
user = "…@smtp-brevo.com"
password = "…"
from = "Telos <telos@example.org>"

[brevo]
api_key = "xkeysib-…"

[recordatio]
url = "https://recordatio.example.org"
api_key = "…"

[recordatio.scopes."telos-owner-homey"]
user_id = "owner"
slug = "homey"
```

## Semantics

**`mail.send`.** The `Message-ID` is `<{effect_key}@{sender-domain}>`. `effect_key` is Cardea's scope key, so
a retry after "not applied" reuses the identity. `OperationRejected` (FAILED, no effect, retry allowed) is raised for:
- an invalid recipient address, or missing SMTP configuration, before connecting;
- a connection failure, a STARTTLS failure or an authentication failure;
- the sender or recipient being refused (any reply before DATA);
- an explicit 5xx reply to the message body.

A dropped connection or timeout during or after DATA, or a 4xx reply to DATA, propagates. Cardea then marks
the effect `outcome_unknown`.

**`mail.send.reconcile`.** Calls `GET https://api.brevo.com/v3/smtp/statistics/events?messageId=…&days=90` with
the `api-key` header.
- Any event for the Message-ID means **applied**, and the output carries the latest event.
- No events means **not applied**, but only once 10 minutes have passed since the operation deadline, because
  Brevo indexes events with a delay. Before then, the reconciler raises and the effect stays uncertain.
- API errors and malformed responses raise.

**`mail.preview`.** Takes the same input and validation as `mail.send` but never connects. It returns
`{"preview": true, "to", "subject", "message_id": "<{effect_key}@preview>"}` for shadow-mode comparison.

**Reads.** When coverage is missing, the listing and search operations return `status: "unavailable"` with a
reason instead of failing, so the agent never reads the gap as zero inventory.

## Dependencies cut from Telos

- The HTML parsers (`telos/transport/browser/{rightmove,zoopla,agencies}.py`, `telos/domain/agencies.py`) are
  copied verbatim into `telos_cardea/_vendor/`. Only one import line differs. `tests/test_vendor_sync.py` fails
  when a copy drifts.
- `telos.transport.browser.fetcher` runs a curl-vs-Chromium race on every fetch. Here it is replaced by a
  sequential curl → unblocker → optional browser fetch. Image download, `Pillow` and OnTheMarket
  enrichment are dropped. Outputs carry structured listings, not page text or images.
- `telos.mailer` and `EmailNotificationService` are replaced by a direct `smtplib` sender with stage-aware
  error mapping. The Telos `Settings` object is replaced by environment variables and a TOML file.
- `ToolContext.user_id` and `agent_slug` (from the user registry) are replaced, for `memory.recall`, by a
  configured mapping from Cardea namespace to Recordatio scope. `RecordatioFeedbackClient` is inlined as one httpx call.
- The per-turn `BrowserSession` is replaced by stateless calls, so `browser.click` takes a `url`.
  Popups, downloads (PDF and ODT extraction), `browser_fill`, `browser_read` and `browser_back` are not
  packaged.
- The `telos.transport.search` engines are re-implemented for Serper, SerpAPI and DDGS. Gemini grounding
  (which needs `google-genai`) is not packaged, and `SEARCH_ENGINE=gemini` uses the free path.
- `property_candidates` is not packaged because it is bound to Telos's SQLite repository.

## Development

```sh
cd packages/telos-cardea
uv sync                    # dev tools; tests validate descriptors with tests/_cardea_operations.py
uv sync --group cardea     # optional: real Cardea from the pinned private commit
uv run ruff check . && uv run mypy && uv run pytest -q
```

Or run `make cardea-ops-check` from the repo root.
