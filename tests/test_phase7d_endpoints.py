"""Backend endpoints introduced by PR 7D (Login + Profile pages).

Covers the JSON / SPA-facing routes added to
:mod:`telos.api.routes.google_oauth`:

- ``GET  /api/oauth/google/start``       — alias of the legacy SSR start.
- ``POST /api/oauth/google/disconnect``  — JSON-returning disconnect.
- ``GET  /api/user/google``              — connection state for the SPA.

The existing ``/api/auth/me``, ``/api/user/email`` (GET + POST), and
``/api/user/settings`` endpoints are exercised by ``test_auth_dual.py``
and the legacy SSR test suite; we don't duplicate that coverage here.
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def srv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Same rig as ``test_auth_dual.py`` — hermetic registry under tmp_path.

    Repointing ``_registry`` + ``_agent_runtime`` keeps each test's
    Google-creds writes isolated from other tests in the same process.
    """
    from telos.api import app as _app
    from telos.api.registry import AgentRegistry

    monkeypatch.setattr(_app.settings, "storage_root", tmp_path)
    monkeypatch.setattr(_app.settings, "public_host", "")
    fresh = AgentRegistry(tmp_path / "registry.db")
    monkeypatch.setattr(_app, "_registry", fresh)
    monkeypatch.setattr(_app, "_agent_runtime", _app._build_agent_runtime())

    client = TestClient(_app.app, follow_redirects=False)
    return client, _app


def _bearer_for(srv_module, uid: str) -> dict[str, str]:
    token = srv_module._signer.dumps(uid)
    return {"Authorization": f"Bearer {token}"}


# ──────────────────────────────────────────────────────────────────────
# /api/oauth/google/start
# ──────────────────────────────────────────────────────────────────────


def test_api_oauth_start_requires_auth(srv):
    client, _ = srv
    r = client.get("/api/oauth/google/start")
    assert r.status_code == 401


def test_api_oauth_start_returns_503_when_unconfigured(srv, monkeypatch: pytest.MonkeyPatch):
    """No client_id / redirect_uri → 503 with a helpful detail.

    Mirrors the SSR start's behaviour so a misconfigured prod doesn't
    silently 500 from inside ``build_authorize_url``.
    """
    client, _app = srv
    monkeypatch.setattr(_app.settings, "google_oauth_client_id", "")
    monkeypatch.setattr(_app.settings, "google_oauth_redirect_uri", "")
    r = client.get("/api/oauth/google/start", headers=_bearer_for(_app, "alice"))
    assert r.status_code == 503
    assert "not configured" in r.json()["detail"].lower()


def test_api_oauth_start_303_to_google_with_signed_state(srv, monkeypatch: pytest.MonkeyPatch):
    """Configured → 303 to Google's consent screen with a signed state."""
    client, _app = srv
    monkeypatch.setattr(_app.settings, "google_oauth_client_id", "test-client-id")
    monkeypatch.setattr(
        _app.settings,
        "google_oauth_redirect_uri",
        "http://localhost/oauth/google/callback",
    )

    r = client.get("/api/oauth/google/start", headers=_bearer_for(_app, "alice"))
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("https://accounts.google.com/o/oauth2/v2/auth")
    assert "client_id=test-client-id" in loc
    assert "state=" in loc

    # State must verify back to ``alice`` with the right purpose tag.
    # Pull the param out of the URL and round-trip it through the signer.
    from urllib.parse import parse_qs, urlparse
    qs = parse_qs(urlparse(loc).query)
    state = qs["state"][0]
    payload = _app._signer.loads(state)
    assert payload == {"uid": "alice", "purpose": "google_oauth"}


# ──────────────────────────────────────────────────────────────────────
# /api/oauth/google/disconnect
# ──────────────────────────────────────────────────────────────────────


def test_api_oauth_disconnect_requires_auth(srv):
    client, _ = srv
    r = client.post("/api/oauth/google/disconnect")
    assert r.status_code == 401


def test_api_oauth_disconnect_returns_json_and_clears_creds(srv):
    """Unlike the SSR sibling, the SPA disconnect returns JSON (no redirect)."""
    client, _app = srv
    _app._registry.set_google_credentials("alice", "refresh-token", "a@example.com")
    assert _app._registry.get_google_credentials("alice") == ("refresh-token", "a@example.com")

    r = client.post("/api/oauth/google/disconnect", headers=_bearer_for(_app, "alice"))
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    refresh, email = _app._registry.get_google_credentials("alice")
    assert not refresh and not email


# ──────────────────────────────────────────────────────────────────────
# /api/user/google
# ──────────────────────────────────────────────────────────────────────


def test_api_user_google_requires_auth(srv):
    client, _ = srv
    r = client.get("/api/user/google")
    assert r.status_code == 401


def test_api_user_google_reports_unconfigured(srv, monkeypatch: pytest.MonkeyPatch):
    """Missing client_id/redirect_uri → ``configured: false``.

    The SPA uses this to hide the Connect button and show a helpful
    'add these env vars' note.
    """
    client, _app = srv
    monkeypatch.setattr(_app.settings, "google_oauth_client_id", "")
    monkeypatch.setattr(_app.settings, "google_oauth_redirect_uri", "")

    r = client.get("/api/user/google", headers=_bearer_for(_app, "alice"))
    assert r.status_code == 200
    assert r.json() == {"configured": False, "email": None}


def test_api_user_google_reports_configured_but_not_connected(srv, monkeypatch: pytest.MonkeyPatch):
    client, _app = srv
    monkeypatch.setattr(_app.settings, "google_oauth_client_id", "x")
    monkeypatch.setattr(_app.settings, "google_oauth_redirect_uri", "http://localhost/cb")

    r = client.get("/api/user/google", headers=_bearer_for(_app, "alice"))
    assert r.status_code == 200
    assert r.json() == {"configured": True, "email": None}


def test_api_user_google_reports_connected_account(srv, monkeypatch: pytest.MonkeyPatch):
    client, _app = srv
    monkeypatch.setattr(_app.settings, "google_oauth_client_id", "x")
    monkeypatch.setattr(_app.settings, "google_oauth_redirect_uri", "http://localhost/cb")
    _app._registry.set_google_credentials("alice", "refresh-token", "a@example.com")

    r = client.get("/api/user/google", headers=_bearer_for(_app, "alice"))
    assert r.status_code == 200
    assert r.json() == {"configured": True, "email": "a@example.com"}
