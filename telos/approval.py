"""Manage candidate prompt staging, approval, and rollback."""
from collections.abc import Callable
from pathlib import Path

from telos.improver import CandidatePrompt


class ApprovalManager:
    def __init__(
        self,
        prompts_dir: Path,
        on_improvement_ready: Callable[[CandidatePrompt], None] | None = None,
    ) -> None:
        self._dir = prompts_dir
        self._on_ready = on_improvement_ready
        self._candidate: CandidatePrompt | None = None
        prompts_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Staging
    # ------------------------------------------------------------------

    def stage(self, candidate: CandidatePrompt) -> None:
        self._candidate = candidate
        (self._dir / f"{candidate.sha}.txt").write_text(candidate.content)
        (self._dir / "candidate.txt").write_text(candidate.content)
        if self._on_ready:
            self._on_ready(candidate)

    @property
    def has_candidate(self) -> bool:
        return (self._dir / "candidate.txt").exists()

    @property
    def pending(self) -> CandidatePrompt | None:
        return self._candidate

    # ------------------------------------------------------------------
    # Admin actions
    # ------------------------------------------------------------------

    def approve(self) -> str:
        """Deploy staged candidate. Returns new prompt content."""
        if self._candidate is not None:
            content = self._candidate.content
        else:
            path = self._dir / "candidate.txt"
            if not path.exists():
                raise RuntimeError("No candidate prompt staged.")
            content = path.read_text()

        (self._dir / "current.txt").write_text(content)
        (self._dir / "candidate.txt").unlink(missing_ok=True)
        self._candidate = None
        return content

    def reject(self) -> None:
        self._candidate = None
        (self._dir / "candidate.txt").unlink(missing_ok=True)

    def rollback(self, sha: str) -> str:
        """Revert to a specific past prompt version."""
        path = self._dir / f"{sha}.txt"
        if not path.exists():
            raise FileNotFoundError(f"No prompt version with sha={sha!r}")
        content = path.read_text()
        (self._dir / "current.txt").write_text(content)
        return content

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def ensure_version(self, sha: str, content: str) -> None:
        """Write a versioned copy of a prompt if it doesn't already exist."""
        versioned = self._dir / f"{sha}.txt"
        if not versioned.exists():
            versioned.write_text(content)

    def load_current_prompt(self, fallback: str) -> str:
        path = self._dir / "current.txt"
        if path.exists():
            return path.read_text()
        # First time: save fallback as v1
        path.write_text(fallback)
        return fallback
