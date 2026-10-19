"""Authenticated internal transport for the optional isolated coding worker."""
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from pontifex.codex import CodexRunConfig


@dataclass
class WorkerOutcome:
    structured_output: Any = None
    error: str | None = None
    cost_usd: float = 0


class WorkerSession:
    def __init__(self, url: str, token: str, provider: str) -> None:
        self._url = url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._provider = provider
        self.cleanup_safe = True

    async def _request(self, endpoint: str, payload: dict, timeout_s: float) -> dict:
        async with httpx.AsyncClient(base_url=self._url, headers=self._headers, timeout=timeout_s, trust_env=False) as client:
            try:
                response = await client.post(endpoint, json=payload)
                response.raise_for_status()
                return response.json()
            except BaseException:
                # Do not remove shared files while the worker might still be using them.
                self.cleanup_safe = False
                try:
                    response = await asyncio.shield(client.delete(f"/jobs/{payload['job_id']}", timeout=15))
                    response.raise_for_status()
                    self.cleanup_safe = response.json().get("stopped") is True
                except (httpx.HTTPError, ValueError, asyncio.CancelledError):
                    pass
                raise

    async def preflight(self, workdir: Path) -> str | None:
        return (await self._request("/preflight", {"job_id": workdir.name, "provider": self._provider}, 45)).get("error")

    async def run(self, *, config, system_prompt: str, prompt: str) -> WorkerOutcome:
        result = await self._request("/execute", {
            "job_id": Path(config.cwd).name, "provider": self._provider, "model": config.model,
            "system_prompt": system_prompt, "prompt": prompt,
            "max_turns": config.max_tool_calls if isinstance(config, CodexRunConfig) else config.max_turns,
            "total_timeout_s": config.total_timeout_s,
        }, config.total_timeout_s + 45)
        return WorkerOutcome(**result)
