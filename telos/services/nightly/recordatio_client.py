"""Async HTTP client for Recordatio's nightly read + feedback endpoints.

Thin wrapper over the routes shipped in Recordatio's nightly-feedback API
(units / memory-search / memory-feedback) plus the existing briefing-events
lookup. The nightly analyst's read-tools call these; the service posts the
reduce-phase memory feedback. Auth mirrors ``telos.agent``: a bearer key on
every request.
"""
from typing import Any

import httpx


class RecordatioFeedbackClient:
    """Calls Recordatio's user-scoped read + feedback endpoints."""

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 60.0) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        # base_url ends at the host; we issue absolute /v1 paths.
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "RecordatioFeedbackClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def list_units(self, user_id: str, *, day: str | None = None, limit: int = 200) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if day:
            params["day"] = day
        r = await self._http.get(f"/v1/users/{user_id}/units", params=params)
        r.raise_for_status()
        return r.json()

    async def memory_search(
        self,
        user_id: str,
        query: str,
        *,
        mode: str = "production",
        slug: str | None = None,
        as_of: str | None = None,
        k: int = 10,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"query": query, "mode": mode, "k": k}
        if slug:
            params["slug"] = slug
        if as_of:
            params["as_of"] = as_of
        r = await self._http.get(f"/v1/users/{user_id}/memory-search", params=params)
        r.raise_for_status()
        return r.json()

    async def get_briefing(self, request_id: str) -> dict[str, Any]:
        """What Recordatio recalled for a turn (units/entities), by request id.

        Returns ``{}`` on 404 — a turn may have no persisted briefing (e.g.
        an old turn, or one run with briefing disabled).
        """
        r = await self._http.get(f"/v1/briefing-events/{request_id}")
        if r.status_code == 404:
            return {}
        r.raise_for_status()
        return r.json()

    async def post_memory_feedback(
        self,
        user_id: str,
        *,
        day: str,
        summary: str,
        findings: list[dict[str, Any]],
        ops: list[dict[str, Any]],
    ) -> dict[str, Any]:
        r = await self._http.post(
            f"/v1/users/{user_id}/memory-feedback",
            json={"day": day, "summary": summary, "findings": findings, "ops": ops},
        )
        r.raise_for_status()
        return r.json()


__all__ = ["RecordatioFeedbackClient"]
