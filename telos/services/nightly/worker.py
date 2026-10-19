"""Dedicated coding worker. No API application, scheduler, or application-data mounts."""
import asyncio
import hmac
import os
import re
from contextlib import suppress
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from pontifex.agent import AgentRunConfig, AgentSession
from pontifex.codex import CodexRunConfig, CodexSession
from pydantic import BaseModel, ConfigDict, Field

from telos.services.nightly.implementer import IMPL_OUTPUT_SCHEMA
from telos.services.nightly.sandbox_preflight import check_codex_sandbox


class Job(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(pattern=r"^nightly-impl-[a-zA-Z0-9_-]+$", max_length=100)
    provider: Literal["codex", "claude"]


class Execution(Job):
    model: str = Field(max_length=200)
    system_prompt: str = Field(max_length=100_000)
    prompt: str = Field(max_length=500_000)
    max_turns: int = Field(ge=1, le=500)
    total_timeout_s: float = Field(gt=0, le=5400)


def create_app(*, token: str | None = None, workspace_root: Path | None = None, outside_root: Path | None = None) -> FastAPI:
    # Keep the service credential out of child-process environments.
    token = token if token is not None else os.environ.pop("NIGHTLY_WORKER_TOKEN", "")
    if len(token) < 32:
        raise RuntimeError("NIGHTLY_WORKER_TOKEN must contain at least 32 characters")
    root = (workspace_root or Path(os.environ.get("NIGHTLY_WORKER_WORKSPACE_ROOT", "/nightly-work"))).resolve()
    outside = outside_root or Path("/outside")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    active: dict[str, asyncio.Task] = {}

    def authorize(request: Request) -> None:
        if not hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {token}"):
            raise HTTPException(401, "Invalid worker credential")

    def workspace(job_id: str) -> Path:
        if not re.fullmatch(r"nightly-impl-[a-zA-Z0-9_-]{1,87}", job_id):
            raise HTTPException(400, "Invalid job id")
        path = root / job_id
        if path.is_symlink() or path.resolve().parent != root or not path.is_dir():
            raise HTTPException(400, "Job workspace does not exist or escapes the job root")
        return path

    async def bounded(request, job, operation):
        if active:
            raise HTTPException(409, "Worker already has an active job")
        task = asyncio.create_task(operation())
        active[job.job_id] = task
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=0.5)
                if await request.is_disconnected():
                    task.cancel()
                    raise HTTPException(499, "Caller disconnected")
            return await task
        finally:
            if not task.done():
                task.cancel()
            try:
                with suppress(asyncio.CancelledError):
                    await task
            finally:
                active.pop(job.job_id, None)

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "busy": bool(active)}

    @app.delete("/jobs/{job_id}")
    async def cancel(job_id: str, request: Request):
        authorize(request)
        task = active.get(job_id)
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        return {"stopped": True}

    @app.post("/preflight")
    async def preflight(job: Job, request: Request):
        authorize(request)
        path = workspace(job.job_id)

        async def probe():
            error = await check_codex_sandbox(path, outside_root=outside) if job.provider == "codex" else None
            return {"error": error}

        return await bounded(request, job, probe)

    @app.post("/execute")
    async def execute(job: Execution, request: Request):
        authorize(request)
        path = workspace(job.job_id)

        async def run():
            # pip installs the private Pontifex Git dependency in a fresh venv.
            # Git does not consume GITHUB_TOKEN automatically. Scope a helper
            # to github.com and read the token at execution time; never put it
            # in a clone URL, repository config, or a credential-store file.
            env = {
                "GITHUB_TOKEN": os.environ.get("GITHUB_TOKEN", ""),
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "credential.https://github.com.helper",
                "GIT_CONFIG_VALUE_0": '!f() { if [ "$1" = get ]; then printf "username=x-access-token\\npassword=%s\\n" "$GITHUB_TOKEN"; fi; }; f',
            }
            common = dict(model=job.model, cwd=str(path), output_schema=IMPL_OUTPUT_SCHEMA,
                          extra_env=env, total_timeout_s=job.total_timeout_s)
            if job.provider == "codex":
                config = CodexRunConfig(**common, workspace_write=True, network_access=True, max_tool_calls=job.max_turns)
                session = CodexSession()
            else:
                config = AgentRunConfig(**common, max_turns=job.max_turns, allowed_tools=["Read", "Edit", "Write", "Bash", "Glob", "Grep"])
                session = AgentSession()
            try:
                async with asyncio.timeout(job.total_timeout_s + 10):
                    result = await session.run(config=config, system_prompt=job.system_prompt, prompt=job.prompt)
                return {"structured_output": result.structured_output, "error": result.error, "cost_usd": result.cost_usd}
            except TimeoutError:
                return {"structured_output": None, "error": "Coding worker timed out", "cost_usd": 0}

        return await bounded(request, job, run)

    return app
