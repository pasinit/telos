"""Typed accessors for FastAPI application state."""

from typing import Any

from fastapi import Request


def routine_runner(request: Request) -> Any:
    """Return the active routine runner stored by the app lifespan."""
    return getattr(request.app.state, "routine_runner", None)
