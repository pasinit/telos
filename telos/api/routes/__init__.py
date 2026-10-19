"""Route modules for the Telos API.

Each module owns one cohesive group of JSON endpoints (auth, oauth,
memories, ...) and exposes a ``router`` ``APIRouter``. ``app.py``
includes them via ``for r in routers: app.include_router(r)``.
"""
from telos.api.routes import (
    agents,
    auth,
    chat,
    costs,
    google_oauth,
    improvements,
    memories,
    nightly,
    routines,
    settings_page,
)

routers = [
    auth.router,
    google_oauth.router,
    memories.router,
    chat.router,
    agents.router,
    settings_page.router,
    routines.router,
    costs.router,
    improvements.router,
    nightly.router,
]

__all__ = ["routers"]
