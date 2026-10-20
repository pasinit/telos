# Claude Instructions

## Project

Telos (τέλος) hosts persistent, goal-oriented agents. The package lives at `telos/`. A FastAPI JSON API under `telos/api/` serves the chat, agent settings, and usage data; the Svelte SPA under `telos-web/` is the frontend. Agents talk to Recordatio (HTTP proxy + memory store) over an OpenAI-compatible SSE interface. See `README.md` for the full architecture.

Key modules:

- `telos/agent.py` — chat loop, tool dispatch, Recordatio client
- `telos/improver.py` — self-improving system prompts (research → propose → judge)
- `telos/routines.py` — scheduled tasks via APScheduler
- `telos/log.py` — SQLite persistence (conversations, costs, prompt history)
- `telos/approval.py` — candidate prompt staging
- `telos/tools.py`, `telos/browser.py`, `telos/mcp.py` — tools available to agents
- `telos/api/app.py` — FastAPI factory + lifespan + module-level singletons; routes under `telos/api/routes/` (JSON only)
- `telos-web/` — Svelte SPA frontend (Vite + svelte-spa-router + vite-plugin-pwa). Components under `src/components/`, pages under `src/pages/`. Conventions in `telos-web/CONVENTIONS.md`.

## Project Tracking

Use GitHub issues on `pasinit/telos` as the single source of truth for work tracking and planning. Before starting any non-trivial task, check open issues for context. When implementing a feature or fix, reference the relevant issue in commit messages. New work that isn't captured in an issue should be logged as one before proceeding.

Recordatio is tracked separately in `pasinit/recordatio`. If a feature requires changes on both sides, file an issue in each repo and cross-link them.

## Code Style

Write idiomatic Python. The project requires Python ≥ 3.13, so all modern type syntax is available natively. Concretely:

- **No `from __future__ import annotations`** — postponed evaluation is not needed; use native syntax (`X | Y`, `list[str]`, `str | None`). A few legacy files still import it (`agent.py` neighbours such as `improver.py`, `signals.py`, `browser.py`, `housekeeper.py`, `google_auth.py`) — remove the import whenever you touch one of these files.
- Use built-in generic types (`list`, `dict`, `tuple`, `set`) instead of `typing.List`, `typing.Dict`, etc.
- Use `X | Y` union syntax instead of `typing.Union[X, Y]` and `X | None` instead of `typing.Optional[X]`.
- Prefer `pathlib.Path` over `os.path`.
- Ruff line length is 150 (`pyproject.toml`).

## Development

```bash
# set up
make install            # creates .venv and installs with [dev,ui] extras

# daily workflow
make test               # pytest tests/ -v
make dev                # start FastAPI (:7433) + Vite (:5173) — one command, traps Ctrl+C
make run                # start the FastAPI backend only (serves the built SPA from telos-web/dist if present)
make recordatio         # start Recordatio from ../recordatio
make web-build          # build telos-web/dist for production
make web-dev            # start Vite dev server only

# Docker / prod
make docker-build
make docker-up
make deploy             # runs deploy.sh
```

The chat UI is a Svelte SPA under `telos-web/`. When changing UI, run `make dev` and exercise the change in the browser before declaring done — type checks and unit tests do not catch UX regressions.

## Related Repos

- **Recordatio** (`pasinit/recordatio`) — LLM proxy that injects briefings and extracts knowledge. Telos depends on it via `RECORDATIO_URL`.
- **Cardea** (`pasinit/cardea`) — durable execution kernel that routines are migrating to; see `telos/cardea_bridge.py` and `packages/telos-cardea/`.
- A provider-routing library (private) is pinned in `pyproject.toml`.
