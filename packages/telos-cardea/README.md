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
| `browser.open` | `browser_open` | read | none | none | **confined worker** (`descriptors/confined/`) |
| `browser.click` | `browser_click` | read | none | none | **confined worker** (`descriptors/confined/`) |
| `memory.recall` | `memory_recall` | read | none | none | in-process |

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
also needs the confined profile; the `listings.*` variants in `descriptors/confined/` enable it. Without it,
agencies whose cards are filled in client-side (Dexters prices) use their server HTML, and anti-bot walls
report `status: "unavailable"`.

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
install -m 0600 -o cardea packages/telos-cardea/descriptors/{mail.send,web.search,memory.recall}.json /etc/cardea/operations/
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
