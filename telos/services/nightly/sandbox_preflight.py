"""Verify Codex command isolation before spending a model turn on implementation."""
import asyncio
import sys
import tempfile
from pathlib import Path

from pontifex.codex_client import CodexClient

_PROBE = '''import errno, pathlib, subprocess, sys
outside = pathlib.Path(sys.argv[1])
try:
    outside.write_text("forbidden")
except OSError as exc:
    if exc.errno not in (errno.EACCES, errno.EPERM, errno.EROFS):
        raise
else:
    raise SystemExit("sandbox allowed a write outside the workspace")
pathlib.Path("probe.txt").write_text("sandbox probe\\n")
for args in (["init", "-q"], ["add", "probe.txt"],
             ["-c", "user.name=Sandbox Probe", "-c", "user.email=probe@localhost", "commit", "-qm", "probe"]):
    subprocess.run(["git", *args], check=True, capture_output=True)
print("TELOS_SANDBOX_PREFLIGHT_OK")
'''


async def check_codex_sandbox(workdir: Path, *, timeout_s: float = 30, outside_root: Path | None = None) -> str | None:
    """Return a blocking reason, or None after real file/Git/denial checks pass.

    command/exec uses the same sandbox policy as the implementer, without
    creating a model turn. The outside canary is writable by this process, so
    denial tests the inner sandbox, not a read-only container mount.
    """
    try:
        with tempfile.TemporaryDirectory(prefix=".sandbox-probe-", dir=workdir) as directory:
            probe = Path(directory)
            with tempfile.TemporaryDirectory(prefix="telos-sandbox-denial-", dir=outside_root or Path.home()) as outside_dir:
                outside = Path(outside_dir) / "canary"
                outside.write_text("unchanged")
                client = CodexClient(cwd=str(workdir), timeout_s=timeout_s, extra_env={"GITHUB_TOKEN": ""})
                try:
                    async with asyncio.timeout(timeout_s):
                        await client.connect()
                        result = await client.request("command/exec", {
                            "command": [sys.executable, "-c", _PROBE, str(outside)],
                            "cwd": str(probe), "timeoutMs": int(timeout_s * 1000), "outputBytesCap": 4000,
                            "sandboxPolicy": {"type": "workspaceWrite", "writableRoots": [str(workdir)], "networkAccess": True},
                        })
                finally:
                    await client.disconnect()
                if outside.read_text() != "unchanged":
                    return "Codex sandbox preflight failed: outside-workspace write was permitted"
                if result.get("exitCode") != 0 or "TELOS_SANDBOX_PREFLIGHT_OK" not in result.get("stdout", ""):
                    detail = (result.get("stderr") or result.get("stdout") or "missing command result").strip()[-2000:]
                    return f"Codex sandbox preflight failed (exit {result.get('exitCode')}): {detail}"
                if not (probe / "probe.txt").is_file() or not (probe / ".git" / "refs" / "heads").is_dir():
                    return "Codex sandbox preflight failed: workspace/Git artifacts missing"
                return None
    except TimeoutError:
        return f"Codex sandbox preflight timed out after {timeout_s:g}s"
    except (OSError, RuntimeError, ValueError) as exc:
        return f"Codex sandbox preflight unavailable: {type(exc).__name__}: {exc}"
