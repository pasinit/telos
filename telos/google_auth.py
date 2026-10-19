"""Per-user Google OAuth flow and credentials cache.

Each Telos user connects their own Google account from /profile. This module:

  • Builds the consent URL (build_authorize_url).
  • Exchanges the OAuth callback code for a refresh token + the connected
    email (exchange_code).
  • Mints/refreshes per-user access tokens on demand and caches the
    Credentials object in process memory (get_credentials), so subsequent
    tool calls within the same process don't pay the refresh round-trip
    every time.

Refresh tokens are persisted plaintext in user_settings via the registry
helpers — same trust model as the existing settings on the droplet's
SQLite DB. Encrypting at rest keyed off Settings.secret_key is a documented
follow-up.
"""
import logging
import threading
from typing import Any
from urllib.parse import urlencode

import httpx
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

log = logging.getLogger(__name__)

GOOGLE_OAUTH_SCOPES = [
    "https://www.googleapis.com/auth/drive.metadata.readonly",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "openid",
    "email",
]

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


def build_authorize_url(state: str, client_id: str, redirect_uri: str) -> str:
    """Construct Google's consent screen URL.

    The caller signs `state` (typically with the existing URLSafeSerializer)
    and verifies it on the callback. This module is auth-agnostic about the
    state value — just round-trips it.
    """
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(GOOGLE_OAUTH_SCOPES),
        "access_type": "offline",      # required to get a refresh_token
        "prompt": "consent",           # ensures refresh_token is returned
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{_AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str, client_id: str, client_secret: str, redirect_uri: str) -> tuple[str, str]:
    """Exchange an authorisation code for (refresh_token, email).

    Calls Google's token endpoint synchronously (httpx) — fine for the
    callback handler since this only runs once per Connect click.
    """
    payload = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    with httpx.Client(timeout=20) as client:
        resp = client.post(_TOKEN_URL, data=payload)
        resp.raise_for_status()
        token_data = resp.json()
        refresh_token = token_data.get("refresh_token", "")
        if not refresh_token:
            # Most commonly Google withholds refresh_token if the user
            # already granted consent; prompt=consent in the auth URL
            # avoids that, but just in case:
            raise RuntimeError(
                "Google did not return a refresh_token. Revoke previous "
                "consent at https://myaccount.google.com/permissions and "
                "try again."
            )

        # Fetch the connected email so we can show it on /profile.
        access_token = token_data.get("access_token", "")
        userinfo = client.get(
            _USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        userinfo.raise_for_status()
        email = userinfo.json().get("email", "")
    return refresh_token, email


# ---------------------------------------------------------------------
# Per-user Credentials cache
# ---------------------------------------------------------------------


class GoogleAuthCache:
    """Encapsulates the per-user Google Credentials cache and its lock.

    A single default instance (``_default_cache``) backs the module-level
    ``get_credentials`` and ``invalidate`` functions so callers don't need
    to change. Construct a fresh instance in tests to get isolated state.
    """

    def __init__(self) -> None:
        self._cache: dict[str, Any] = {}
        self._lock = threading.Lock()

    def get_credentials(self, user_id: str, registry: Any, client_id: str, client_secret: str):
        """Return a refreshed google.oauth2.credentials.Credentials, or None.

        Returns None if the user hasn't connected their Google account or if
        the refresh token has been revoked. The ``Credentials`` object is
        cached per user_id; google-auth handles silent re-refresh internally
        when access tokens expire.
        """
        refresh_token, _email = registry.get_google_credentials(user_id)
        if not refresh_token:
            return None

        with self._lock:
            creds = self._cache.get(user_id)
            if creds is not None:
                # If the refresh token in the DB no longer matches the cached
                # one (e.g. user disconnected and reconnected with a different
                # account), drop the cached entry.
                if getattr(creds, "refresh_token", None) != refresh_token:
                    self._cache.pop(user_id, None)
                    creds = None
            if creds is None:
                try:
                    creds = Credentials(
                        token=None,
                        refresh_token=refresh_token,
                        token_uri=_TOKEN_URL,
                        client_id=client_id,
                        client_secret=client_secret,
                        scopes=GOOGLE_OAUTH_SCOPES,
                    )
                    self._cache[user_id] = creds
                except Exception:
                    log.exception("failed to construct Google Credentials for %s", user_id)
                    return None

        # Refresh if needed (token expired or never minted).
        try:
            if not creds.valid:
                creds.refresh(Request())
        except Exception:
            log.warning("Google credentials refresh failed for %s — token revoked?", user_id)
            # Drop cache so next call re-evaluates after the user reconnects.
            with self._lock:
                self._cache.pop(user_id, None)
            return None

        return creds

    def invalidate(self, user_id: str) -> None:
        """Drop the cached Credentials for a user (called on Disconnect)."""
        with self._lock:
            self._cache.pop(user_id, None)


# Module-level default instance — used by the thin wrappers below so
# existing callers require no changes.
_default_cache = GoogleAuthCache()


def get_credentials(user_id: str, registry: Any, client_id: str, client_secret: str):
    """Return a refreshed google.oauth2.credentials.Credentials, or None.

    Thin wrapper around ``GoogleAuthCache.get_credentials`` on the
    module-level default cache instance.
    """
    return _default_cache.get_credentials(user_id, registry, client_id, client_secret)


def invalidate(user_id: str) -> None:
    """Drop the cached Credentials for a user (called on Disconnect)."""
    _default_cache.invalidate(user_id)
