"""SPA static-serve mount for ``telos-web/dist``.

Always on post-Phase-7K — the SSR Jinja layer is gone, so the SPA shell
is the only HTML the app serves. The flag (``TELOS_SPA_ENABLED``) and
the per-page incremental cutover are no longer needed.

Resolution rules:

1. ``/assets/...``  → served from ``telos-web/dist/assets`` by a
   :class:`StaticFiles` mount. Vite emits hashed filenames here, so
   long-cache headers are safe.
2. ``/api/...``, ``/oauth/...``, ``/docs``, ``/openapi.json``, ``/redoc``,
   ``/healthz`` → 404 from the catch-all (so they fall through to the
   JSON / OAuth-callback / FastAPI-built-in handlers registered earlier).
3. Any other path with a matching file in ``dist/`` (e.g.
   ``/manifest.webmanifest``, ``/icon-dark.svg``, ``/sw.js``) →
   that file.
4. Everything else → ``dist/index.html``. ``svelte-spa-router`` runs
   client-side and resolves the actual route.

The catch-all is registered LAST in :func:`register`, after every JSON
router. FastAPI tries routes in declaration order, so the API endpoints
match first.
"""

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# Paths the SPA catch-all must NOT shadow. Keep in sync with
# navigateFallbackDenylist in telos-web/vite.config.ts so the server and
# the service worker agree on what "not SPA territory" means.
_BACKEND_PREFIXES: tuple[str, ...] = (
    "api/",
    "oauth/",
    "docs",
    "openapi.json",
    "redoc",
    "healthz",
)


def _resolve_dist() -> Path:
    """Locate ``telos-web/dist`` relative to this file.

    Override via ``TELOS_SPA_DIST`` if the layout differs (tests use
    this to point at a tmp_path).
    """
    override = os.getenv("TELOS_SPA_DIST", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (Path(__file__).resolve().parent.parent.parent / "telos-web" / "dist").resolve()


def register(app: FastAPI) -> bool:
    """Register the SPA mount + catch-all on ``app``.

    Returns ``True`` if the SPA was wired up, ``False`` if the ``dist/``
    tree is missing (e.g. dev server started without ``make web-build``).
    In that case the API still works but ``GET /`` returns 404 — the
    intended dev workflow is ``make dev``, which runs Vite alongside.

    MUST be called AFTER every ``include_router(...)`` so the catch-all
    only fires for paths nothing else claimed.
    """
    dist = _resolve_dist()
    if not dist.is_dir() or not (dist / "index.html").is_file():
        return False

    assets = dist / "assets"
    if assets.is_dir():
        app.mount(
            "/assets",
            StaticFiles(directory=assets),
            name="spa-assets",
        )

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str) -> FileResponse:
        # Backend-owned namespaces: refuse so they fall through to the
        # handlers registered before us.
        for prefix in _BACKEND_PREFIXES:
            if full_path == prefix.rstrip("/") or full_path.startswith(prefix):
                raise HTTPException(status_code=404)

        # Bare file under dist/ — manifest.webmanifest, sw.js, icons.
        if full_path:
            candidate = (dist / full_path).resolve()
            try:
                candidate.relative_to(dist)
            except ValueError:
                raise HTTPException(status_code=404) from None
            if candidate.is_file():
                return FileResponse(candidate)

        # SPA shell — svelte-spa-router handles client routing.
        return FileResponse(dist / "index.html")

    return True
