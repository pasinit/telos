"""Tests for the SPA static-serve mount (PR 7B).

The catch-all in :mod:`telos.api.spa` is gated by ``TELOS_SPA_ENABLED``
and only fires for paths nothing else has claimed. These tests use a
freshly-built :class:`FastAPI` app (NOT the module-level
``telos.api.app.app``) so we don't depend on the prod app's lifespan +
singletons, and a synthetic ``dist/`` tree under ``tmp_path``.
"""
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from telos.api import spa

# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────


def _seed_dist(root: Path) -> Path:
    """Create a minimal vite-style ``dist/`` tree under ``root``.

    Mirrors what ``vite-plugin-pwa`` actually emits: ``index.html``, an
    ``assets/`` dir with a hashed JS file, plus a few PWA artefacts
    that live at the root (``manifest.webmanifest``, ``sw.js``,
    ``icon-dark.svg``).
    """
    dist = root / "telos-web" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><div id=app></div>")
    (dist / "assets" / "index-abc123.js").write_text("console.log('spa')")
    (dist / "manifest.webmanifest").write_text('{"name":"Telos"}')
    (dist / "sw.js").write_text("// service worker")
    (dist / "icon-dark.svg").write_text("<svg/>")
    return dist


def _build_app() -> FastAPI:
    """Stand up a FastAPI with the SSR routes the catch-all must NOT shadow.

    Mirrors the ``telos/ui/app.py`` mount order: SSR routes first,
    then the SPA catch-all (via ``spa.register``). The handlers
    themselves are stubs — we only care about which path wins.
    """
    app = FastAPI()

    @app.get("/login", response_class=PlainTextResponse)
    async def ssr_login() -> str:
        return "SSR LOGIN"

    @app.get("/api/me")
    async def api_me() -> dict[str, str]:
        return {"user": "tomma"}

    @app.get("/api/usage")
    async def api_usage() -> dict[str, int]:
        return {"total": 0}

    @app.get("/oauth/google/start")
    async def oauth_start() -> dict[str, str]:
        return {"url": "https://google.com/..."}

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    return app


@pytest.fixture
def dist_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Synthetic ``dist/`` + ``TELOS_SPA_DIST`` env override."""
    dist = _seed_dist(tmp_path)
    monkeypatch.setenv("TELOS_SPA_DIST", str(dist))
    return dist


# ──────────────────────────────────────────────────────────────────────
# register() — always-on post-7K; no-op when dist tree missing
# ──────────────────────────────────────────────────────────────────────


def test_register_noop_when_dist_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """dist missing → silent no-op so dev boot still works without `make web-build`."""
    monkeypatch.setenv("TELOS_SPA_DIST", str(tmp_path / "does-not-exist"))
    app = _build_app()
    assert spa.register(app) is False

    client = TestClient(app)
    assert client.get("/agents/foo").status_code == 404


def test_register_noop_when_index_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A dist directory without index.html is incomplete — refuse to mount."""
    dist = tmp_path / "telos-web" / "dist"
    dist.mkdir(parents=True)
    (dist / "assets").mkdir()  # only assets, no index.html
    monkeypatch.setenv("TELOS_SPA_DIST", str(dist))

    app = _build_app()
    assert spa.register(app) is False


def test_register_serves_spa_shell(dist_tmp: Path) -> None:
    """dist present → unknown paths serve index.html."""
    app = _build_app()
    assert spa.register(app) is True

    client = TestClient(app)
    r = client.get("/agents/foo")
    assert r.status_code == 200
    assert "<div id=app></div>" in r.text


def test_ssr_routes_still_win_under_flag_on(dist_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SSR routes registered before spa.register() must still match.

    This is the core mount-order invariant: per-page PRs (7D–7J) keep
    their existing SSR endpoints until they rewrite them, so the
    catch-all only fires for unclaimed paths.
    """
    monkeypatch.setenv("TELOS_SPA_ENABLED", "1")
    app = _build_app()
    spa.register(app)

    client = TestClient(app)
    r = client.get("/login")
    assert r.status_code == 200
    assert r.text == "SSR LOGIN"


def test_api_paths_not_shadowed(dist_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The catch-all must NOT serve the SPA shell for /api, /oauth, /static."""
    monkeypatch.setenv("TELOS_SPA_ENABLED", "1")
    app = _build_app()
    spa.register(app)

    client = TestClient(app)

    # Registered /api/me → JSON
    r = client.get("/api/me")
    assert r.status_code == 200
    assert r.json() == {"user": "tomma"}

    # Unregistered /api/nope → 404 (NOT SPA shell). This is the
    # critical regression target: a buggy catch-all would return
    # index.html here, breaking every "did this endpoint actually
    # 404?" check in the front-end.
    r = client.get("/api/nope")
    assert r.status_code == 404
    assert "<div id=app></div>" not in r.text

    # /oauth/* belongs to FastAPI even when unregistered — refusing to
    # serve the SPA shell means the redirect dance to Google can't
    # accidentally pick up an HTML payload.
    r = client.get("/oauth/google/callback")
    assert r.status_code == 404
    assert "<div id=app></div>" not in r.text


def test_healthz_not_shadowed(dist_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """/healthz must keep returning JSON for the Docker HEALTHCHECK."""
    monkeypatch.setenv("TELOS_SPA_ENABLED", "1")
    app = _build_app()
    spa.register(app)

    client = TestClient(app)
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_root_serves_spa_shell(dist_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``/`` → index.html (the SPA boots from there)."""
    monkeypatch.setenv("TELOS_SPA_ENABLED", "1")
    app = _build_app()
    spa.register(app)

    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    assert "<div id=app></div>" in r.text


def test_root_level_static_files_served(dist_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Vite emits ``manifest.webmanifest``, ``sw.js``, icons at /, not /assets.

    The catch-all serves them directly when they exist as files in
    dist/, instead of falling through to the SPA shell.
    """
    monkeypatch.setenv("TELOS_SPA_ENABLED", "1")
    app = _build_app()
    spa.register(app)

    client = TestClient(app)

    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert '"name":"Telos"' in r.text

    r = client.get("/sw.js")
    assert r.status_code == 200
    assert "service worker" in r.text

    r = client.get("/icon-dark.svg")
    assert r.status_code == 200
    assert "<svg/>" in r.text


def test_assets_mount_serves_hashed_files(dist_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``/assets/<hash>.js`` is served by the StaticFiles mount."""
    monkeypatch.setenv("TELOS_SPA_ENABLED", "1")
    app = _build_app()
    spa.register(app)

    client = TestClient(app)
    r = client.get("/assets/index-abc123.js")
    assert r.status_code == 200
    assert "console.log('spa')" in r.text


def test_path_traversal_rejected(dist_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``../etc/passwd`` style escapes must not be served as plain files."""
    monkeypatch.setenv("TELOS_SPA_ENABLED", "1")
    app = _build_app()
    spa.register(app)

    client = TestClient(app)
    # The TestClient normalises some of these, but the resolve() +
    # relative_to() check is the real guard. Verify the catch-all
    # never reaches outside dist/.
    r = client.get("/..%2f..%2fetc%2fpasswd")
    # Either 404 from the catch-all guard or the SPA shell — but
    # absolutely never the contents of /etc/passwd.
    assert r.status_code in (200, 404)
    if r.status_code == 200:
        assert "root:" not in r.text  # nothing system-y leaked
        assert "<div id=app></div>" in r.text  # SPA shell fallback


def test_unknown_route_under_known_prefix_returns_shell(dist_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``/profile`` (no SSR handler in this test rig) → SPA shell.

    This is the canonical case for a page that's been migrated to the
    SPA: SSR removed, catch-all serves the shell, client router takes
    over.
    """
    monkeypatch.setenv("TELOS_SPA_ENABLED", "1")
    app = _build_app()
    spa.register(app)

    client = TestClient(app)
    r = client.get("/profile")
    assert r.status_code == 200
    assert "<div id=app></div>" in r.text
