"""Auth boundary helpers for Phase 7.

Token issue/verify (``tokens``) and cookie helpers (``cookies``) live
here so :mod:`telos.api.deps` and the auth routes don't reach into each
other's internals. The signer instance itself lives on
``telos.api.app._signer`` (a singleton bound to ``settings.secret_key``);
helpers in this package take it as a parameter so they stay testable.
"""

from telos.api.auth import cookies, tokens

__all__ = ["cookies", "tokens"]
