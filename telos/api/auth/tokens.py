"""Issue and verify auth tokens.

Tokens are opaque strings produced by :class:`itsdangerous.URLSafeSerializer`
over the user's username, signed with ``settings.secret_key``. Same
crypto as today's signed cookie (locked decision in Phase 7 plan §1.2)
— no new key material, no migration story.

Lifetime is enforced by the cookie's ``Max-Age`` (1 year) and not by
the token itself, so this module deliberately does NOT use
``URLSafeTimedSerializer``. A leaked token is valid until the user
rotates ``SECRET_KEY`` (no prefix; unset means a random key per process, so every restart logs everyone out).
"""

from itsdangerous import BadSignature, URLSafeSerializer

# Header name. FastAPI's ``Header(...)`` parameter normalises case, so
# the kwarg name in the route handler is ``authorization``; this
# constant exists for documentation + the explicit parser below.
AUTHORIZATION_HEADER = "Authorization"


def issue(signer: URLSafeSerializer, username: str) -> str:
    """Sign ``username`` and return the opaque token string.

    The token is the same string that goes into the ``user_id`` cookie
    AND into JSON login responses for Bearer-header consumers.
    """
    return signer.dumps(username)


def verify(signer: URLSafeSerializer, token: str) -> str | None:
    """Verify ``token`` and return the embedded username, or ``None``.

    Returns ``None`` for:
    - ``BadSignature`` (tampered token, or signed by a different key)
    - non-string payloads (defensive; shouldn't happen with our issuer)
    - empty / whitespace-only payloads
    """
    try:
        payload = signer.loads(token)
    except BadSignature:
        return None
    if not isinstance(payload, str):
        return None
    payload = payload.strip()
    if not payload:
        return None
    return payload


def extract_token(cookie: str | None, authorization: str | None) -> str | None:
    """Pull the auth token from the cookie or ``Authorization`` header.

    Bearer wins if both are present — this lets a CLI override a stale
    browser cookie deliberately. Empty / whitespace-only values are
    treated as missing.

    Parsing rules for ``Authorization``:
    - Scheme is case-insensitive (``Bearer``, ``bearer``, ``BEARER``).
    - ``split(None, 1)`` is used so ``"Bearer  doublespace"`` and tab
      separators are tolerated (still RFC 6750 compliant).
    - The token portion must be non-empty after stripping.
    """
    if authorization:
        parts = authorization.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            tok = parts[1].strip()
            if tok:
                return tok
    if cookie and cookie.strip():
        return cookie.strip()
    return None


__all__ = ["AUTHORIZATION_HEADER", "issue", "verify", "extract_token"]
