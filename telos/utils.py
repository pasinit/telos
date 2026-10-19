"""Shared utilities for Telos."""
import re


def slugify(name: str) -> str:
    """Convert a name to a URL-friendly slug."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


# Single source of truth for what a telos identifier (user_id / agent
# slug) may look like — but note the two consumers enforce it at
# DIFFERENT points in the lifecycle:
#
# - ``telos.api.routes.auth`` gates the charset at SIGNUP only (the
#   first login, which mints the user_id). Users that already exist in
#   the registry are admitted even if their id predates the charset
#   rule — rejecting at every login would lock them out of their own
#   account (they get a logged warning instead).
# - ``telos.mcp.server._validate_id`` stays STRICT on every request —
#   MCP path components are spliced into filesystem paths under
#   ``storage_root``, so nothing outside this charset may pass. That's
#   safe precisely because the signup gate means every id minted from
#   now on conforms; a grandfathered non-conforming id simply has no
#   MCP surface.
#
# ``.``, ``@`` and ``+`` are admitted so email-style usernames work
# (including plus-addressing à la ``user+tag@gmail.com``); ``/`` stays
# banned (path separator), ``:`` stays banned (it's the HMAC separator
# in ``derive_agent_token``), and :func:`is_safe_id` additionally
# rejects a leading dot (hidden files / ``.`` / ``..`` traversal
# segments).
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.@+-]{1,64}$")

SAFE_ID_DESCRIPTION = (
    "1-64 characters from [A-Za-z0-9_.@+-], not starting with '.'"
)


def is_safe_id(value: object) -> bool:
    """True iff ``value`` is a string safe to use as a single path segment.

    Beyond the charset/length regex, explicitly rejects ``..`` (path
    traversal) and any id starting with ``.`` — the regex admits dots so
    email-style ids work, so the dot-position rules live here.
    """
    if not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value):
        return False
    if value == "..":  # explicit traversal guard (also caught below;
        return False   # kept separate so the intent is unmissable)
    if value.startswith("."):
        return False
    return True
