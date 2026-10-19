"""Cookie helpers for the auth boundary.

Centralises the attribute set so :mod:`telos.api.routes.auth` (legacy
form-POST + new JSON login) and any future caller can't get the
attributes wrong. SameSite=Lax is the locked choice; the only knob
that varies per environment is ``Secure``, which is derived from
``PUBLIC_HOST``: HTTPS deploys get ``Secure=True``; localhost dev (HTTP)
gets ``Secure=False``.

Why this matters: Chrome silently drops ``Secure`` cookies on HTTP, so
hardcoding ``secure=True`` would break local dev; hardcoding
``secure=False`` would leak cookies on HTTP in prod misconfigurations.
"""

from fastapi import Response

COOKIE_NAME = "user_id"
MAX_AGE_SECONDS = 60 * 60 * 24 * 365  # 1 year — mirrors pre-Phase-7 behaviour
SAMESITE: str = "lax"
COOKIE_PATH = "/"


def is_secure(public_host: str) -> bool:
    """Return True iff the deployment URL is HTTPS.

    Reads from ``settings.public_host`` (``PUBLIC_HOST`` env). Empty or
    HTTP-prefixed values yield ``False`` so localhost dev keeps working.
    """
    return public_host.strip().lower().startswith("https://")


def set_auth_cookie(response: Response, token: str, *, public_host: str = "") -> None:
    """Set the signed-auth cookie on ``response``.

    The token is whatever :func:`telos.api.auth.tokens.issue` produced —
    an opaque string. Caller is responsible for issuing the token; this
    helper only owns the cookie attributes.
    """
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=MAX_AGE_SECONDS,
        httponly=True,
        samesite=SAMESITE,
        secure=is_secure(public_host),
        path=COOKIE_PATH,
    )


def delete_auth_cookie(response: Response) -> None:
    """Clear the signed-auth cookie on ``response``.

    ``delete_cookie`` must match the ``path`` of the original
    ``set_cookie`` — pass ``path="/"`` so the browser actually removes
    the cookie rather than setting a path-scoped null one.
    """
    response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH)


__all__ = [
    "COOKIE_NAME",
    "COOKIE_PATH",
    "MAX_AGE_SECONDS",
    "SAMESITE",
    "is_secure",
    "set_auth_cookie",
    "delete_auth_cookie",
]
