VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
WEB := telos-web

.PHONY: install test run recordatio venv lint-imports \
        web-install web-dev web-build web-test web-check dev \
        docker-build docker-up docker-down docker-logs docker-restart deploy \
        shell prod-up caddy-logs caddy-reload recordatio-hash \
        aspire aspire-stop jaeger jaeger-stop

# ── Local dev ──────────────────────────────────────────────────────────

venv:
	python3.13 -m venv $(VENV)

install: venv
	$(PIP) install -e ".[dev,ui]"

# Regenerate the pinned runtime lockfile the Docker image installs from. Run
# after changing runtime dependencies in pyproject.toml, then commit both.
# No lockfile = every rebuild re-resolves and can pull a breaking major
# (an unpinned mcp>=1.27 pulled mcp 2.x and crash-looped prod, 2026-09-08).
lock:
	uv pip compile pyproject.toml --extra ui --python-version 3.13 -o requirements.txt

test:
	$(VENV)/bin/pytest tests/ -v

lint-imports:
	$(VENV)/bin/lint-imports

# Start a local Aspire dashboard (OTel collector + UI on :18888).
# Once running, set ``export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317``
# in the shell that runs ``make run`` / ``make dev`` to enable backend tracing.
# For the SPA, also set
# ``export VITE_OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318/v1/traces``
# before starting Vite.
aspire:
	docker compose --profile tracing up -d aspire
	@echo "Aspire UI: http://localhost:18888"
	@echo "Backend export: export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317"
	@echo "Browser export: export VITE_OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318/v1/traces"
	@echo "Browser CORS origins: $${OTEL_BROWSER_CORS_ALLOWED_ORIGINS:-http://localhost:5173,http://127.0.0.1:5173}"

aspire-stop:
	docker compose --profile tracing down aspire

# Start a local Jaeger sidecar (OTel collector + UI on :16686).
# Once running, set ``export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317``
# in the shell that runs ``make run`` to enable tracing.
jaeger:
	docker compose --profile tracing up -d jaeger
	@echo "Jaeger UI: http://localhost:16686"
	@echo "Backend export: export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317"
	@echo "Browser export: export VITE_OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318/v1/traces"
	@echo "Browser CORS origins: $${OTEL_BROWSER_CORS_ALLOWED_ORIGINS:-http://localhost:5173,http://127.0.0.1:5173}"

jaeger-stop:
	docker compose --profile tracing down jaeger

run:
	$(VENV)/bin/telos-api

recordatio:
	$(MAKE) -C ../recordatio run

# ── Frontend (telos-web) ───────────────────────────────────────────────

web-install:
	cd $(WEB) && npm install

web-dev:
	cd $(WEB) && npm run dev

web-build:
	cd $(WEB) && npm run build

web-test:
	cd $(WEB) && npm test

web-check:
	cd $(WEB) && npm run check

# Run backend (FastAPI on :7433) and frontend (Vite on :5173) together.
# Trap-wrapped: Ctrl+C kills BOTH children. Vite proxies /api -> :7433
# (see telos-web/vite.config.ts) so the SPA talks to the local backend.
#
# Notes on the trap:
# - No `set -m`: monitor mode would put each `&` job in its own pgid,
#   which breaks `kill 0`. Without it, children inherit the parent
#   bash's pgid, so `kill 0` reaches them all.
# - Track explicit PIDs and SIGTERM them too, so npm's grandchildren
#   (vite) tear down even if `kill 0` would otherwise miss them on
#   some platforms.
dev:
	@echo "→ starting telos-api (backend, :7433) and vite (frontend, :5173)"
	@bash -c '\
		pids=""; \
		cleanup() { echo; echo "→ stopping…"; [ -n "$$pids" ] && kill $$pids 2>/dev/null; kill 0 2>/dev/null; wait 2>/dev/null; exit 0; }; \
		trap cleanup INT TERM EXIT; \
		$(VENV)/bin/telos-api & pids="$$pids $$!"; \
		( cd $(WEB) && exec npm run dev ) & pids="$$pids $$!"; \
		wait'

# ── Docker ─────────────────────────────────────────────────────────────

docker-build:
	GITHUB_TOKEN=$(GITHUB_TOKEN) docker compose build

docker-up:
	GITHUB_TOKEN=$(GITHUB_TOKEN) docker compose up -d

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f

docker-restart:
	docker compose down telos && docker compose up -d telos

shell:
	docker compose exec telos bash

# ── Prod (Caddy / HTTPS) ───────────────────────────────────────────────

prod-up:
	GITHUB_TOKEN=$(GITHUB_TOKEN) COMPOSE_PROFILES=prod docker compose up -d

caddy-logs:
	docker compose logs -f caddy

caddy-reload:
	docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile

# Generate a bcrypt hash for the Recordatio dashboard password. Set the
# resulting hash as RECORDATIO_PASSWORD_HASH in .env. Username is "recordatio".
# Usage: make recordatio-hash PASS=mysecret
recordatio-hash:
	@test -n "$(PASS)" || (echo "Usage: make recordatio-hash PASS=<password>"; exit 1)
	@docker run --rm caddy:2-alpine caddy hash-password --plaintext "$(PASS)"

deploy:
	./deploy.sh
