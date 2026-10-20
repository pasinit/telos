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
| `memory.briefing` | (agent-hook briefing) | read | none | none | in-process |
| `telos.routine_context` | (routine-run context) | read | none | none | in-process |
| `ledger.list` | `list_property_candidates` | read | none | none | in-process |
| `ledger.history` | `list_property_candidates` with `url` | read | none | none | in-process |
| `ledger.record` | `record_property_assessment` | effect | `["/url"]` (one assessment per listing per idempotency context) | `ledger.record.reconcile` | in-process |
| `ledger.record_preview` | (shadow mode) | read | none | none | in-process |

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
install -m 0600 -o cardea packages/telos-cardea/descriptors/{mail.send,web.search,memory.recall,memory.briefing}.json /etc/cardea/operations/
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
| Recordatio base URL | `RECORDATIO_URL` | `recordatio.url` | `memory.recall`, `memory.briefing` |
| Recordatio bearer key (a recall key) | `CHAT_API_KEY` | `recordatio.api_key` | `memory.recall`, `memory.briefing` |
| Cardea (namespace, agent) → Recordatio user/agent | none | `recordatio.scopes."<namespace>/<agent_id>"` (fallback: `"<namespace>"`) | `memory.recall`, `memory.briefing` |
| Telos internal API base URL | `TELOS_INTERNAL_URL` | `telos.url` | `telos.routine_context`, `ledger.*` |
| JWT-SVID file for Telos (audience `telos-internal`) | `TELOS_INTERNAL_TOKEN_FILE` | `telos.token_file` | `telos.routine_context`, `ledger.*` |
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

# Key: "<Cardea namespace>/<agent_id>". The Telos bridge uses namespace "telos-<user>" and agent "telos-<slug>".
[recordatio.scopes."telos-owner/telos-homey"]
user_id = "owner"
slug = "homey"

[telos]
url = "http://telos.internal.example:7445"
token_file = "/run/telos-cardea/telos-internal.jwt"
```

The token file is re-read on every call, so the spiffe-helper that rewrites it before expiry needs no
coordination. A bare `"<namespace>"` scope key still works and applies to every agent in that namespace;
prefer the `"<namespace>/<agent_id>"` form.

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

**Telos scope.** `telos.routine_context` and the `ledger.*` operations send `X-Cardea-Namespace` (the task's
namespace), `X-Cardea-Agent` (the context's `agent_id`) and, for trigger-admitted runs, `X-Cardea-Slot-Time`
(the context's `slot_time`). None of them ever comes from tool arguments. A call without an `agent_id` (outside
an agent run, or on a Cardea that does not yet provide it) is rejected before any request. Telos answers 404
when that agent has no mirrored routine, which surfaces as `OperationRejected` naming the namespace and agent.
Missing `telos.url` or token file, an unreadable or empty token file, an unreachable listener, 401/403, 409 and
422 are also `OperationRejected`.

**`ledger.record`.** `POST /internal/cardea/v1/ledger/assessments` with the assessment as the body and
`Idempotency-Key: {effect_key}`. Because the scope is `["/url"]`, `effect_key` is the same for every call about one
listing in one idempotency context (a trigger slot), so a retry or a repeated call returns the first assessment
id (`duplicate: true`) instead of recording again. A 422 (Telos's cross-field rules, such as "verified needs every
criterion passing") or a 409 (a business rule, such as moving an assessment out of `applied`) is
`OperationRejected`. A timeout, a connection dropped after sending, or a 5xx propagates, and Cardea marks the effect
`outcome_unknown`. The input schema is Telos's `PropertyAssessmentInput`; `tests/test_ledger.py` rebuilds that
model from the Telos source and fails when the descriptors drift.

**`ledger.record.reconcile`.** `GET /internal/cardea/v1/ledger/assessments/by-key/{effect_key}` with the same scope
headers.
- 200 means **applied**, and the output carries the stored `assessment_id` (`duplicate: true`).
- 404 means **not applied**. Telos commits the key with the assessment, so a missing key means nothing was stored.
- Any other status raises and the effect stays uncertain.

**`ledger.record_preview`.** The same request with `?dry_run=1`: Telos validates the assessment and stores
nothing. It returns `{"preview": true, "recorded": false, "valid": true, "url"}`, and an invalid assessment fails
exactly as it would live, so shadow runs see the same errors.

**`memory.briefing`.** `GET {recordatio.url}/v1/users/{user_id}/briefing?slug=…&query=…` with the recall key. It
returns `{"briefing", "units"}`, the text the agent hook would inject, and Recordatio writes nothing. Like
`memory.recall`, the user and slug come from `recordatio.scopes`, never from arguments.

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
- `ToolContext.user_id` and `agent_slug` (from the user registry) are replaced, for `memory.*`, by a
  configured mapping from Cardea (namespace, agent) to Recordatio scope, and for `ledger.*` by Telos's own
  mapping of the `X-Cardea-*` headers. `RecordatioFeedbackClient` is inlined as one httpx call.
- The per-turn `BrowserSession` is replaced by stateless calls, so `browser.click` takes a `url`.
  Popups, downloads (PDF and ODT extraction), `browser_fill`, `browser_read` and `browser_back` are not
  packaged.
- The `telos.transport.search` engines are re-implemented for Serper, SerpAPI and DDGS. Gemini grounding
  (which needs `google-genai`) is not packaged, and `SEARCH_ENGINE=gemini` uses the free path.
- `property_candidates` stays in Telos, bound to its SQLite repository. The `ledger.*` operations reach it through
  Telos's internal API instead of opening the database.

## Development

```sh
cd packages/telos-cardea
uv sync                    # dev tools; tests validate descriptors with tests/_cardea_operations.py
uv sync --group cardea     # optional: real Cardea from the pinned private commit
uv run ruff check . && uv run mypy && uv run pytest -q
```

Or run `make cardea-ops-check` from the repo root.
