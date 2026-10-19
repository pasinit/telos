"""Dual-source auth matrix for PR 7C.

Required matrix per plan §7C:

1. cookie-only        → returns the user (200)
2. bearer-only        → returns the user (200)
3. both present       → bearer wins (intentional override)
4. neither            → 401 on /api/* (303 on non-API for legacy SSR)
5. expired signature  → 401 (signed by a rotated/different key)
6. tampered signature → 401 (signature bytes altered)

Plus the JSON-API contract: ``POST /api/auth/login`` returns
``{token, user}`` AND sets the cookie; ``GET /api/auth/me`` works with
both auth modes; ``POST /api/auth/logout`` clears the cookie.

We use a fresh in-process app via the existing ``server`` style
fixture so we don't depend on prod ``_signer``/registry state, and so
``URLSafeSerializer`` rotation tests don't pollute the production signer.
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from itsdangerous import URLSafeSerializer

# ──────────────────────────────────────────────────────────────────────
# Test rig
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def srv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Yield the freshly-pointed ``telos.api.app`` module + a TestClient.

    Mirrors the rig in ``tests/test_usage.py`` — repoints
    ``storage_root`` + ``_registry`` at ``tmp_path`` so individual tests
    are hermetic.
    """
    from telos.api import app as _app
    from telos.api.registry import AgentRegistry

    monkeypatch.setattr(_app.settings, "storage_root", tmp_path)
    monkeypatch.setattr(_app.settings, "public_host", "")  # http://localhost default
    fresh = AgentRegistry(tmp_path / "registry.db")
    monkeypatch.setattr(_app, "_registry", fresh)
    monkeypatch.setattr(_app, "_agent_runtime", _app._build_agent_runtime())

    client = TestClient(_app.app, follow_redirects=False)
    return client, _app


def _cookie_for(srv_module, username: str) -> str:
    """Issue a cookie value (signed by the prod signer)."""
    return srv_module._signer.dumps(username)


# ──────────────────────────────────────────────────────────────────────
# Required matrix — cases 1–6
# ──────────────────────────────────────────────────────────────────────


def test_cookie_only_authenticates(srv):
    """1. cookie-only → 200, returns user."""
    client, _app = srv
    cookie = _cookie_for(_app, "alice")
    r = client.get("/api/auth/me", cookies={"user_id": cookie})
    assert r.status_code == 200
    assert r.json() == {"uid": "alice"}


def test_bearer_only_authenticates(srv):
    """2. bearer-only → 200, returns user (no cookie sent)."""
    client, _app = srv
    token = _app._signer.dumps("alice")
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json() == {"uid": "alice"}


def test_bearer_wins_over_cookie_when_both_present(srv):
    """3. both present → bearer wins.

    A CLI may want to override a stale browser cookie by setting a
    fresh ``Authorization`` header — the dep MUST honour the header
    rather than fall back to the cookie. Implementation: bearer is
    checked first in ``tokens.extract_token``.
    """
    client, _app = srv
    cookie_token = _app._signer.dumps("alice")
    bearer_token = _app._signer.dumps("bob")
    r = client.get(
        "/api/auth/me",
        cookies={"user_id": cookie_token},
        headers={"Authorization": f"Bearer {bearer_token}"},
    )
    assert r.status_code == 200
    assert r.json() == {"uid": "bob"}, "bearer should win, not cookie"


def test_neither_returns_401_on_api(srv):
    """4a. neither → 401 on /api/* paths (machine-readable for SPA + CLI)."""
    client, _ = srv
    r = client.get("/api/auth/me")
    assert r.status_code == 401
    detail = r.json().get("detail", "")
    assert "Not authenticated" in detail
    assert "missing" in detail  # reason suffix


def test_expired_signature_returns_401(srv):
    """5. expired signature → 401.

    "Expired" in our model = signed by a key that's no longer in use
    (the team rotated ``SECRET_KEY``). The opaque token format
    has no built-in TTL — lifetime is enforced by the cookie's
    ``Max-Age``. So a stale token is one with a non-matching signature.
    """
    client, _ = srv
    # Build a token with a DIFFERENT signer — simulates a rotated key.
    other_signer = URLSafeSerializer("a-completely-different-secret-key")
    stale_token = other_signer.dumps("alice")

    # As cookie
    r = client.get("/api/auth/me", cookies={"user_id": stale_token})
    assert r.status_code == 401
    assert "invalid" in r.json()["detail"]

    # As bearer
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {stale_token}"})
    assert r.status_code == 401
    assert "invalid" in r.json()["detail"]


def test_tampered_signature_returns_401(srv):
    """6. tampered signature → 401.

    Flip the FIRST character of a valid token's signature; the
    ``URLSafeSerializer`` rejects it with ``BadSignature`` and we
    surface 401. (Not the last char: base64 decoding discards the
    trailing 2 bits of the 27-char SHA1 signature, so ~6% of last-char
    flips decode to the identical signature and the test flaked.)
    """
    client, _app = srv
    good = _app._signer.dumps("alice")
    # The signature is the bit after the last '.'. Flip its first char.
    head, _, sig = good.rpartition(".")
    first = sig[0]
    flipped = ("A" if first != "A" else "B") + sig[1:]
    bad = f"{head}.{flipped}"

    r = client.get("/api/auth/me", cookies={"user_id": bad})
    assert r.status_code == 401
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {bad}"})
    assert r.status_code == 401


# ──────────────────────────────────────────────────────────────────────
# Bearer-parsing edge cases
# ──────────────────────────────────────────────────────────────────────


def test_bearer_scheme_case_insensitive(srv):
    """``bearer``, ``BEARER``, ``Bearer`` all accepted."""
    client, _app = srv
    token = _app._signer.dumps("alice")
    for scheme in ("Bearer", "bearer", "BEARER", "BeArEr"):
        r = client.get("/api/auth/me", headers={"Authorization": f"{scheme} {token}"})
        assert r.status_code == 200, f"failed for scheme={scheme!r}"


def test_bearer_tolerates_whitespace_in_separator(srv):
    """Multiple spaces / tabs between scheme and token are accepted."""
    client, _app = srv
    token = _app._signer.dumps("alice")
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer  {token}"})
    assert r.status_code == 200
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer\t{token}"})
    assert r.status_code == 200


@pytest.mark.parametrize(
    "header",
    [
        "Bearer",  # no token
        "Bearer ",  # empty token
        "Basic dXNlcjpwYXNz",  # wrong scheme
        "garbage",  # no scheme
        "",  # empty (will be sent as no header by httpx)
    ],
)
def test_malformed_bearer_returns_401_not_500(srv, header):
    """Malformed Authorization headers must 401, never 500.

    Critical regression target — a naive ``.split(' ')`` would crash on
    several of these. We use ``.split(None, 1)`` and check ``len==2``.
    """
    client, _ = srv
    headers = {"Authorization": header} if header else {}
    r = client.get("/api/auth/me", headers=headers)
    assert r.status_code == 401, f"got {r.status_code} for header={header!r}"


def test_empty_cookie_treated_as_missing(srv):
    """An empty/whitespace ``user_id`` cookie is treated as absent (401)."""
    client, _ = srv
    r = client.get("/api/auth/me", cookies={"user_id": ""})
    assert r.status_code == 401


# ──────────────────────────────────────────────────────────────────────
# JSON login / logout / me — contract
# ──────────────────────────────────────────────────────────────────────


def test_api_login_returns_token_and_user_and_sets_cookie(srv):
    """``POST /api/auth/login`` issues a token, sets the cookie, returns both."""
    client, _ = srv
    r = client.post("/api/auth/login", json={"username": "alice"})
    assert r.status_code == 200
    body = r.json()
    assert "token" in body and body["token"]
    assert body["user"] == {"uid": "alice"}

    # Cookie set
    cookies = r.headers.get_list("set-cookie")
    assert any("user_id=" in c for c in cookies)
    # SameSite=Lax, HttpOnly, Path=/, Max-Age=31536000 attributes
    cookie_line = next(c for c in cookies if "user_id=" in c)
    assert "HttpOnly" in cookie_line
    assert "Path=/" in cookie_line
    assert "Max-Age=31536000" in cookie_line
    # SameSite encoding is case-insensitive in starlette; check both
    assert "samesite=lax" in cookie_line.lower()


def test_api_login_normalises_username(srv):
    """Whitespace + case-folding so ``Alice``/``alice  `` resolve to the same uid."""
    client, _ = srv
    r = client.post("/api/auth/login", json={"username": "  Alice  "})
    assert r.status_code == 200
    assert r.json()["user"] == {"uid": "alice"}


def test_api_login_rejects_empty_username(srv):
    """Pydantic ``min_length=1`` blocks empty username → 422 (not 500)."""
    client, _ = srv
    r = client.post("/api/auth/login", json={"username": ""})
    # Pydantic validation produces a 422, not a 400 — both are
    # acceptable; what matters is "not 500" and "no token issued".
    assert r.status_code in (400, 422)


def test_api_login_accepts_email_style_username(srv):
    """Email-style usernames (``.``/``@``) are legitimate ids — they
    must round-trip through login AND be servable by the MCP layer
    (shared charset: ``telos.utils.SAFE_ID_RE``)."""
    from telos.utils import is_safe_id

    client, _ = srv
    r = client.post("/api/auth/login", json={"username": "  Owner@Example.com "})
    assert r.status_code == 200
    uid = r.json()["user"]["uid"]
    assert uid == "owner@example.com"
    # The minted uid is exactly what the MCP path validator admits.
    assert is_safe_id(uid)


@pytest.mark.parametrize(
    "username",
    [
        "a/b",            # path separator
        "../etc",         # traversal
        "..",             # traversal segment
        ".hidden",        # leading dot
        "user name",      # whitespace
        "user:colon",     # HMAC separator
        "x" * 65,         # too long
    ],
)
def test_api_login_rejects_unsafe_usernames(srv, username):
    """Signups must not mint ids the MCP layer would reject — the user
    would authenticate fine and then get zero tools forever."""
    client, _ = srv
    r = client.post("/api/auth/login", json={"username": username})
    assert r.status_code == 400, (username, r.text)


def test_api_login_accepts_plus_addressed_email_username(srv):
    """``+`` is common in real addresses (``user+tag@gmail.com``) and is
    admitted by ``SAFE_ID_RE`` — it is not the HMAC separator (``:``)
    and not a path character."""
    from telos.utils import is_safe_id

    client, _ = srv
    r = client.post("/api/auth/login", json={"username": "User+Tag@Gmail.com"})
    assert r.status_code == 200
    uid = r.json()["user"]["uid"]
    assert uid == "user+tag@gmail.com"
    assert is_safe_id(uid)


def test_api_login_grandfathers_existing_user_with_legacy_charset(srv, caplog):
    """The charset rule is a SIGNUP gate, not a login gate: a user whose
    id predates the rule (here: a space) must still be able to log into
    their own account. They get admitted with a logged warning; brand-new
    ids with the same charset stay rejected (see the parametrized
    rejection test above)."""
    import logging

    client, _app = srv
    legacy_uid = "legacy user"  # space — outside SAFE_ID_RE
    # Existing user = has registry state (e.g. settings written before
    # the charset rule shipped).
    _app._registry.update_user_settings(legacy_uid, display_name="Legacy")

    with caplog.at_level(logging.WARNING, logger="telos.api.routes.auth"):
        r = client.post("/api/auth/login", json={"username": "  Legacy User "})
    assert r.status_code == 200
    assert r.json()["user"] == {"uid": legacy_uid}
    assert any("grandfathered" in rec.getMessage() for rec in caplog.records)


def test_api_login_email_persisted(srv):
    """Optional ``email`` field flows through to the registry."""
    client, _app = srv
    r = client.post("/api/auth/login", json={"username": "alice", "email": "a@b.com"})
    assert r.status_code == 200
    assert _app._registry.get_user_email("alice") == "a@b.com"


def test_api_me_with_bearer_from_login(srv):
    """End-to-end: login → use the returned token as Bearer → /me works."""
    client, _ = srv
    login_r = client.post("/api/auth/login", json={"username": "alice"})
    token = login_r.json()["token"]
    # New client without the login cookie jar
    me_r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me_r.status_code == 200
    assert me_r.json() == {"uid": "alice"}


def test_api_logout_clears_cookie(srv):
    """``POST /api/auth/logout`` deletes the cookie. Not gated by auth."""
    client, _ = srv
    # Even without auth — logout must be callable so a 401'd client can
    # still clean up its own cookie.
    r = client.post("/api/auth/logout")
    assert r.status_code == 200
    cookies = r.headers.get_list("set-cookie")
    assert any('user_id=""' in c or "user_id=;" in c.replace(" ", "") or "Max-Age=0" in c for c in cookies)


# ──────────────────────────────────────────────────────────────────────
# Cookie Secure attribute — public_host gates it
# ──────────────────────────────────────────────────────────────────────


def test_cookie_secure_off_for_http_public_host(srv):
    """``PUBLIC_HOST`` empty or HTTP → ``Secure`` flag NOT set."""
    client, _ = srv
    # public_host already "" via fixture
    r = client.post("/api/auth/login", json={"username": "alice"})
    cookie_line = next(c for c in r.headers.get_list("set-cookie") if "user_id=" in c)
    assert "Secure" not in cookie_line


def test_cookie_secure_on_for_https_public_host(srv, monkeypatch: pytest.MonkeyPatch):
    """``PUBLIC_HOST=https://...`` → ``Secure`` flag set."""
    client, _app = srv
    monkeypatch.setattr(_app.settings, "public_host", "https://telos.example.com")
    r = client.post("/api/auth/login", json={"username": "alice"})
    cookie_line = next(c for c in r.headers.get_list("set-cookie") if "user_id=" in c)
    assert "Secure" in cookie_line


# ──────────────────────────────────────────────────────────────────────
# Sub-deps compose correctly with bearer.
# ──────────────────────────────────────────────────────────────────────


def test_sub_deps_accept_bearer(srv):
    """``Depends(current_user)`` flows through to downstream endpoints.

    Smoke test: hitting an authed JSON endpoint with Bearer auth should
    not 401 at the boundary. We use ``/api/agents`` (a simple authed
    list endpoint) — past-auth means we got 200 with whatever payload.
    """
    client, _app = srv
    token = _app._signer.dumps("alice")
    r = client.get("/api/agents", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, "bearer should have been accepted"
