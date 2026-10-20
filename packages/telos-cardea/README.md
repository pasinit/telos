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
| `listings.onthemarket_search` | (new; Telos parses OnTheMarket pages in `browser_open`) | read | none | none | in-process (browser fallback: confined) |
| `listings.agency_search` | `agency_search` | read | none | none | in-process (browser fallback: confined) |
| `web.search` | `web_search` | read | none | none | in-process |
| `browser.open` | `browser_open` | read | none | none | **confined worker** (`descriptors/confined/`) |
| `browser.click` | `browser_click` | read | none | none | **confined worker** (`descriptors/confined/`) |
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
profile. Install them only as confined operations (ADR-028): the variants in `descriptors/confined/` declare
`"executor": {"kind": "confined", "profile": "browser", "worker": "telos-…"}` instead of a `handler`, so each call
runs in its own systemd transient unit + bubblewrap sandbox with a memory cap. See [Confined install](#confined-install).

The listing operations do not start a browser by default. They fetch with `curl_cffi`, then use the
unblocker proxy if one is configured. A browser render runs only when `browser_fallback` is enabled, which
also needs the confined profile; the `listings.*` variants in `descriptors/confined/` enable it. Anti-bot
walls report `status: "unavailable"`.

Agencies whose cards are filled in client-side (`js_search`: Dexters, KFH) never use the bare server HTML, which
lacks those fields. Their search pages come from the browser when `browser_fallback` is on and the extra is
installed, else from the unblocker with JavaScript rendering forced on (a `render_js=false` in the template
becomes `true`), else the search reports `status: "unavailable"` with the reason.

`listings.onthemarket_search` reads OnTheMarket's server HTML (its Next.js state carries the cards), so it
needs no browser. It follows only pagination links observed on the page, up to `max_pages` (default 1, 30 cards
a page).

Browser calls are read-only at the network level, in and out of the sandbox:
- the context blocks service workers;
- every request except GET and HEAD is aborted, apart from Cloudflare's challenge endpoints
  (`challenges.cloudflare.com` and paths under `/cdn-cgi/challenge-platform/`), which a visitor must POST to;
- requests to literal private IPs, `localhost` and local-only names are aborted;
- WebSockets are routed to nothing (when patchright supports `route_web_socket`);
- `browser.click` refuses the submit controls of `form[method=post]` (or `formmethod="post"`).

A Cloudflare interstitial (`cf-mitigated: challenge`) gets up to 25 s to clear before the call is rejected.

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

## Confined install

The confined worker sees only `/usr`, a private `/tmp` and the read-only paths its worker definition binds.
It has no resolver: its only egress is the host-owner proxy named in the profile, which Cardea exports as
`HTTPS_PROXY`. Inside it, `browser.*` checks URLs syntactically (literal IPs, `localhost`, local-only names)
and leaves hostnames to the proxy's allow-list, and Chromium is launched with that proxy.

1. Install the extra and Chromium into Cardea's environment, with Chromium outside any home directory:

   ```sh
   uv pip install --python /opt/cardea/apps/api/.venv/bin/python \
     "telos-cardea[browser] @ git+https://github.com/pasinit/telos.git@<sha>#subdirectory=packages/telos-cardea"
   sudo PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright /opt/cardea/apps/api/.venv/bin/patchright install --with-deps chromium
   sudo chmod -R a+rX /opt/ms-playwright
   ```

   The worker runs as uid 65534 with only `/usr` and the bound paths, so the venv's interpreter must live under
   `/usr` (check `readlink -f /opt/cardea/apps/api/.venv/bin/python`; a uv-managed Python under a home directory
   is not reachable) and every bound path must be world-readable.

2. Install the worker definitions as `/etc/cardea-host-workers.json`, owned by root, mode `0600`.
   [`deploy/cardea-host-workers.example.json`](deploy/cardea-host-workers.example.json) is the complete file
   (one `browser` profile, one worker per confined operation); `tests/test_confined.py` checks it against
   Cardea's helper rules and checks that its allow-list covers every source. Its shape:

   ```json
   {
     "profiles": {
       "browser": {
         "memory_mb": 700, "cpu_percent": 150, "tasks": 512, "wall_seconds": 180,
         "scratch_mb": 256, "output_kb": 256, "jit": true,
         "network": {"policy": "allow_list", "proxy": "127.0.0.2:3128", "hosts": ["challenges.cloudflare.com", "rightmove.co.uk", "*.rightmove.co.uk", "…"]}
       }
     },
     "workers": {
       "telos-browser-open": {
         "argv": ["/opt/cardea/apps/api/.venv/bin/python", "-P", "-s", "-m", "cardea.hosts.operation_worker", "browser.open"],
         "profiles": ["browser"],
         "read_only": ["/opt/cardea/apps/api/.venv", "/opt/cardea/apps/api/src", "/opt/ms-playwright",
                       "/etc/ssl/certs", "/etc/fonts/fonts.conf", "/etc/fonts/conf.d"],
         "env": {"PYTHONPATH": "/opt/cardea/apps/api/src", "PLAYWRIGHT_BROWSERS_PATH": "/opt/ms-playwright"}
       }
     }
   }
   ```

   - `wall_seconds` is 180 so the listing variants fit; Cardea stops each call at the lower of the profile's wall
     time and the descriptor's `timeout_seconds` (150 for `browser.*`, 180 for `listings.*`).
   - `jit` is required: V8 and patchright's Node driver need writable-executable memory.
   - `PYTHONPATH` replaces `-I`: Cardea's `apps/api` is not installed into its venv (the service also runs with
     `PYTHONPATH`). `-P` keeps the scratch directory off `sys.path`.
   - The listing workers add `TELOS_CARDEA_BROWSER_FALLBACK=1`.
   - `output_kb: 256` is the helper's maximum. The worker prints `{"output": …}` with ASCII escapes, so the confined
     listing descriptors cap `max_output_bytes` at 192 KiB to stay under it.

3. Run the egress proxy on the profile's loopback address (never `127.0.0.1`) with a hostname allow-list equal
   to the profile's `hosts`. For Squid:

   ```
   http_port 127.0.0.2:3128
   acl telos_browser dstdomain .rightmove.co.uk .zoopla.co.uk .zoocdn.com .onthemarket.com challenges.cloudflare.com
   acl telos_browser dstdomain .foxtons.co.uk .dexters.co.uk .chestertons.co.uk .hamptons.co.uk .ashponsonby.com .kfh.co.uk
   acl telos_browser dstdomain .portico.com .kayandco.com .parkheath.com .marshandparsons.co.uk .openrent.co.uk
   acl Safe_ports port 80 443
   http_access deny !Safe_ports
   http_access allow telos_browser
   http_access deny all
   ```

   | Source | Proxy hosts |
   |---|---|
   | Cloudflare challenge (Zoopla, agencies behind Cloudflare) | `challenges.cloudflare.com` |
   | Rightmove | `rightmove.co.uk`, `*.rightmove.co.uk` |
   | Zoopla (pages and image/asset CDN) | `zoopla.co.uk`, `*.zoopla.co.uk`, `zoocdn.com`, `*.zoocdn.com` |
   | OnTheMarket | `onthemarket.com`, `*.onthemarket.com` |
   | Agencies (`_vendor/agencies.py`) | apex and `*.` of `foxtons.co.uk`, `dexters.co.uk`, `chestertons.co.uk`, `hamptons.co.uk`, `ashponsonby.com`, `kfh.co.uk`, `portico.com`, `kayandco.com`, `parkheath.com`, `marshandparsons.co.uk`, `openrent.co.uk` |

   Third-party script and API hosts that some pages load (analytics, map tiles, a CMS's asset host) are left
   out on purpose; the HTML the parsers read does not need them. If a JS-rendered agency page comes back without
   cards, check the proxy's denied-`CONNECT` log before widening the list. `browser.open` on any other host fails
   with `could not open …: net::ERR_TUNNEL_CONNECTION_FAILED`.

4. Install the confined descriptors **instead of** the in-process ones of the same name:

   ```sh
   install -m 0600 -o cardea-kernel packages/telos-cardea/descriptors/confined/*.json /etc/cardea/operations/
   ```

   Restart Cardea after changing `/etc/cardea-host-workers.json`; the kernel caches the profile listing.

Confined operations read no private file: `/etc/telos-cardea/config.toml` is not visible in the sandbox, so
their settings come only from the worker's `env`:

| Setting | Worker env | Needed by |
|---|---|---|
| Browser fallback for listings | `TELOS_CARDEA_BROWSER_FALLBACK=1` | `listings.*` |
| Chromium location | `PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright` | all |
| Cardea's sources (not installed in its venv) | `PYTHONPATH=/opt/cardea/apps/api/src` | all |
| Unblocker | `TELOS_UNBLOCKER_URL` (optional) | `listings.*` |

Leave the unblocker out of confined workers unless needed: its URL usually carries an API key, worker `env`
values appear on the bubblewrap command line (visible in the host's process list), and its host would have to
join the allow-list.

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
an agent run, or on a Cardea that does not yet provide it) is rejected before any request. Telos answers 404 with
code `no_mirrored_routine` when that agent has no mirrored routine, which surfaces as
`OperationRejected` naming the namespace and agent. Missing `telos.url` or token file, an unreadable or empty
token file, an unreachable listener, 400, 401/403, any other 404, 409 and 422 are also `OperationRejected`;
FastAPI validation details are rendered as `field.path: message`. A 503 (Telos cannot fetch the SPIRE bundle)
propagates as transient.

**`ledger.record`.** `POST /internal/cardea/v1/ledger/assessments` with the assessment as the body and
`Idempotency-Key: {effect_key}`. Because the scope is `["/url"]`, `effect_key` is the same for every call about one
listing in one idempotency context (a trigger slot), so a retry or a repeated call returns the first assessment
id (`duplicate: true`) instead of recording again. A 422 (Telos's validation and ledger rules, such as "verified needs
every criterion passing" or moving an assessment out of `applied`) or a 409 is `OperationRejected`. Telos
answers a replayed key with the original id before validating the body, so a retry still succeeds after the
evidence has aged. A timeout, a connection dropped after sending, or a 5xx propagates, and Cardea marks the effect
`outcome_unknown`. The input schema is Telos's `PropertyAssessmentInput`; `tests/test_ledger.py` rebuilds that
model from the Telos source and fails when the descriptors drift.

**`ledger.record.reconcile`.** `GET /internal/cardea/v1/ledger/assessments/by-key/{effect_key}` with the same scope
headers.
- 200 means **applied**, and the output carries the stored `assessment_id` (`duplicate: true`).
- 404 with code `assessment_not_found` means **not applied**. Telos commits the key with the assessment
  in that agent's ledger, so a missing key means nothing was stored.
- Any other status, including the unmapped-scope 404, raises and the effect stays uncertain.

Error bodies are matched on their stable `code` (top level, or inside a `detail` object). Only a body
without a code is matched on Telos's detail text (`"No mirrored routine for this agent"`,
`"No assessment recorded under this key"`), for servers that predate codes.

**`ledger.list` / `ledger.history`.** `ledger.list` pages the latest assessment per listing (`limit` up to 200,
`next_offset` null on the last page). `ledger.history` returns `{"history": [...]}` for one URL, newest first,
at most 20 per call with `offset`.

**`ledger.record_preview`.** The same request with `?dry_run=1`: Telos validates the assessment (including the
`applied` rule) and stores nothing, not even the key. It returns `{"preview": true, "recorded": false, "valid": true, "url"}`, and an invalid assessment fails
exactly as it would live, so shadow runs see the same errors.

**`memory.briefing`.** `GET {recordatio.url}/v1/users/{user_id}/briefing?slug=…&query=…` with the recall key. It
returns `{"briefing", "units"}`, the text the agent hook would inject, and Recordatio writes nothing. Like
`memory.recall`, the user and slug come from `recordatio.scopes`, never from arguments.

**Reads.** When coverage is missing, the listing and search operations return `status: "unavailable"` with a
reason instead of failing, so the agent never reads the gap as zero inventory.

## Dependencies cut from Telos

- The HTML parsers (`telos/transport/browser/{rightmove,zoopla,onthemarket,agencies}.py`, `telos/domain/agencies.py`)
  are copied verbatim into `telos_cardea/_vendor/`. Two rewrites differ, both listed in `_vendor/__init__.py`:
  - one import line;
  - one fix in `onthemarket.py`. Live pages (checked 2026-10-20) nest the state under `props.pageProps`, which
    Telos's parser misses. The rewrite is the code of pasinit/telos#15; drop it and re-copy once that lands.

  `tests/test_vendor_sync.py` fails when a copy drifts.
- `telos.transport.browser.fetcher` runs a curl-vs-Chromium race on every fetch. Here it is replaced by a
  sequential curl → unblocker → optional browser fetch. Image download and `Pillow` are dropped. Outputs carry structured listings, not page text or images.
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
