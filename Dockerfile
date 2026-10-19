# ----- Stage 1: build the SPA -----
FROM node:20-alpine AS web-builder

WORKDIR /web

# Copy lockfile + package.json first so the npm-install layer caches
# when only source files change. `npm ci` refuses to run without a
# lockfile — that's intentional, it forces deterministic installs.
COPY telos-web/package.json telos-web/package-lock.json ./
RUN npm ci

# Now copy the SPA source. Changes here invalidate the build layer but
# NOT the deps layer above.
COPY telos-web/ ./

RUN npm run build

# Fail the image build now if dist is empty (a silent vite misconfig
# would otherwise only surface at runtime as a 404 on the SPA shell).
RUN test -f /web/dist/index.html


# ----- Stage 2: Python runtime -----
FROM python:3.13-slim AS runtime

WORKDIR /app

ARG GITHUB_TOKEN

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash git ripgrep bubblewrap socat nodejs npm ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ARG CODEX_CLI_VERSION=0.154.0
RUN npm install -g @openai/codex@${CODEX_CLI_VERSION}
ENV PONTIFEX_CODEX_HOME=/root/.codex-telos

COPY pyproject.toml requirements.txt ./
COPY telos/ telos/
COPY scripts/smoke_codex_runtime.py scripts/smoke_codex_runtime.py

# Bring in the built SPA. Path matches what `telos/api/spa.py` resolves
# to relative to `__file__` (../../telos-web/dist).
COPY --from=web-builder /web/dist /app/telos-web/dist

# Install the fully-pinned dependency set from the lockfile, then telos itself
# with --no-deps so the build never re-resolves. This repo has no other lock, so
# an unpinned dep could drift on any rebuild — an unbounded mcp>=1.27 pulled mcp
# 2.x and crash-looped telos on import (2026-09-08). Regenerate requirements.txt
# with `uv pip compile pyproject.toml --extra ui -o requirements.txt` after
# changing dependencies. The GITHUB_TOKEN dance authenticates the pontifex git dep.
RUN git config --global url."https://${GITHUB_TOKEN}@github.com/".insteadOf "https://github.com/" && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir -e . --no-deps && \
    git config --global --unset url."https://${GITHUB_TOKEN}@github.com/".insteadOf && \
    patchright install --with-deps chromium

EXPOSE 7433

# urllib instead of curl to avoid adding a dep just for the healthcheck.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:7433/healthz', timeout=3).status == 200 else 1)" || exit 1

CMD ["telos-api"]
