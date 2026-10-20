# Telos on Codex

Telos selects Claude by default (`TELOS_SUBSCRIPTION_PROVIDER=claude`); set
`TELOS_SUBSCRIPTION_PROVIDER=codex` to opt into Codex. When selected, chat, routines, nightly analysis/synthesis/implementation, prompt generation,
classification, summaries, naming, and prompt improvement use ChatGPT-authenticated
Codex sessions. Compose passes the same provider to Recordatio chat and identity
judging; extraction follows `RECORDATIO_EXTRACTION_MODEL` from Recordatio's own `.env`. API credentials never serve as fallback for Codex sessions.

`CODEX_MODEL` optionally pins a supported Codex model. Empty uses the installed
CLI default. Legacy `MODEL`, `UTILITY_MODEL`, `PROMPT_GEN_MODEL`, and
`NIGHTLY_FEEDBACK_MODEL` remain available when selecting Claude. Python library
constructors retain their existing defaults; the Telos application explicitly
passes the selected provider throughout.

Chat runs through Recordatio's subscription transport. Utility calls and nightly
jobs call the provider-routing client directly, without memory injection. Nightly
tools keep their existing host handlers and schemas. Only the implementer gets
workspace writes and network-enabled shell execution in disposable clones;
chat, utility calls, and analysts do not gain native shell access.

## Authentication and deployment

Both images install `@openai/codex@0.154.0`. Compose persists separate dedicated
homes in `telos_codex-telos` and `telos_codex-memento` volumes (the prefix is the
Compose project name). Authenticate each service before enabling it:

```bash
docker compose run --rm --no-deps -e CODEX_HOME=/root/.codex-telos telos codex login --device-auth
docker compose run --rm --no-deps -e CODEX_HOME=/root/.codex-memento recordatio codex login --device-auth
```

Telos uses `PONTIFEX_CODEX_HOME=/root/.codex-telos`; Recordatio uses
`RECORDATIO_CODEX_HOME=/root/.codex-memento`. Keep these homes free of personal
configuration, plugins and MCP servers. Login must complete with the account
holding the ChatGPT subscription. No API keys are needed for model calls.

Compose sets Recordatio extraction to `anthropic/claude-haiku-5-5` unless
`RECORDATIO_EXTRACTION_MODEL` is set in Telos's `.env`. For a Codex rollout set it
there to `codex/` (CLI default model) or `codex/<model>`. Do not retain an API provider there for a subscription-only rollout.
Before cutover, run `python scripts/smoke_codex_runtime.py` with the authenticated
Telos home and `python scripts/smoke_codex_subscription.py` in Recordatio, then build and restart both services from the intended commits. Verify
health, effective provider settings, a real chat, and a memory-extraction result.

## Switching providers

Claude remains supported. Keep both providers' credentials and model settings;
switching does not require deleting either implementation or authentication home.
The nightly tool definitions are shared: Claude retains `mcp__...` names, while
Codex uses `host__...` dynamic-tool names. Both dispatch to the same handlers.

In the **Telos checkout's `.env`**, set these two values together:

| Selection | `TELOS_SUBSCRIPTION_PROVIDER` | `RECORDATIO_EXTRACTION_MODEL` |
| --- | --- | --- |
| Codex | `codex` | `codex/` (or `codex/<model>`) |
| Claude | `claude` | Unset (Compose default `anthropic/claude-haiku-5-5`) |

Then apply the selection to both running services:

```bash
docker compose up -d --no-build telos recordatio
```

The provider flag controls Telos, Recordatio chat, and identity judging. Extraction
has its own setting: changing only the flag leaves extraction on its previous
provider. Compose reads the extraction override from Telos's `.env`, not from
`../recordatio/.env`.

For Claude, retain Claude-compatible `MODEL`, `UTILITY_MODEL`,
`PROMPT_GEN_MODEL`, and `NIGHTLY_FEEDBACK_MODEL` values and the existing Claude
credentials in each service's environment file. Recordatio's Claude chat transport
uses its stored agent model, or the Claude CLI default when that model is empty;
its `RECORDATIO_SUBSCRIPTION_MODEL` override is used only by Codex. `CODEX_MODEL`
can remain configured when switching to Claude.

Claude compatibility preserves its existing billing paths: chat and nightly
sessions use the Claude SDK/CLI subscription, while legacy utility calls and an
`anthropic/...` extraction model use the Anthropic API and require their existing
API credentials. The provider flag does not convert those legacy API paths into
subscription calls. Compose keeps chat API fallback disabled for both providers.

After switching, check both service health endpoints and run a representative
chat, routine evaluation, and memory extraction. Claude SDK compatibility tests
do not establish that a saved Claude subscription credential is still valid.

## Usage

Codex completion events report the actual model, upstream token counts, and
zero incremental API cost. Cached input is included once in Telos prompt-token
counts. The zero excludes the fixed subscription price. Historical cost rows
remain unchanged. Goal-generation token counts remain approximate.

Utility completion chunks arrive when the Codex turn finishes. Its `max_tokens`
argument is a length hint rather than a hard API limit; wall-clock timeouts still
apply. Unsupported Claude-native tool names fail explicitly; Telos tools keep
their existing MCP names and permissions.
