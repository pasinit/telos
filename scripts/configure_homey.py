"""Offline, backed-up Homey configuration update. Dry-run unless --apply is set."""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path


def load_state(root: Path, user: str, routine_id: str) -> dict:
    if not re.fullmatch(r"[\w-]+", user):
        raise ValueError("Invalid user ID")
    with sqlite3.connect(f"file:{root / 'registry.db'}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        agent = conn.execute("SELECT system_prompt,tools FROM agents WHERE user_id=? AND slug='homey'", (user,)).fetchone()
    with sqlite3.connect(f"file:{root / 'routines.db'}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        routine = conn.execute("SELECT * FROM routines WHERE id=? AND user_id=? AND agent_slug='homey'", (routine_id, user)).fetchone()
    if agent is None or routine is None:
        raise ValueError("Homey/routine not found for the requested owner")
    prompts = root / user / "homey" / "prompts"
    if (prompts / "candidate.txt").exists():
        raise ValueError("Resolve the pending prompt candidate before updating Homey")
    return {"user": user, "routine_id": routine_id, "agent": dict(agent), "routine": dict(routine),
            "current_prompt": (prompts / "current.txt").read_text()}


def fingerprint(state: dict) -> str:
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


def atomic_text(path: Path, text: str) -> None:
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def configure(root: Path, user: str, routine_id: str, prompt: str, routine: str, recipient: str,
              *, apply: bool = False, expected_state: str = "", backup: Path | None = None,
              service_stopped: bool = False) -> dict:
    state = load_state(root, user, routine_id)
    before = fingerprint(state)
    recipients = re.findall(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", state["routine"]["description"])
    if recipient not in recipients:
        raise ValueError("Recipient must already be explicitly authorised in the saved routine")
    tools = json.loads(state["agent"]["tools"])
    for tool in ("record_property_assessment", "list_property_candidates"):
        if tool not in tools:
            tools.append(tool)
    description = routine.rstrip() + f"\n\nAuthorised email recipient: {recipient}\n"
    proposed = {"state_sha256": before, "prompt_chars": len(prompt), "routine_chars": len(description),
                "tools_added": [tool for tool in tools if tool not in json.loads(state["agent"]["tools"])],
                "schedule_unchanged": True, "applied": False}
    if not apply:
        return proposed
    if not service_stopped or backup is None or not expected_state or expected_state != before:
        raise ValueError("Apply requires stopped service/worker, a new backup path and the exact dry-run state hash")
    # Refuse overwriting an existing backup; never include account credentials.
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(state, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    current = root / user / "homey" / "prompts" / "current.txt"
    prompt_written = False
    try:
        with sqlite3.connect(root / "registry.db") as conn:
            conn.execute("ATTACH DATABASE ? AS routines_db", (str(root / "routines.db"),))
            conn.execute("BEGIN IMMEDIATE")
            if fingerprint(load_state(root, user, routine_id)) != before:
                raise ValueError("Configuration changed during update; no changes applied")
            conn.execute("UPDATE agents SET system_prompt=?,tools=? WHERE user_id=? AND slug='homey'",
                         (prompt, json.dumps(tools), user))
            conn.execute("UPDATE routines_db.routines SET description=? WHERE id=? AND user_id=? AND agent_slug='homey'",
                         (description, routine_id, user))
            atomic_text(current, prompt)
            prompt_written = True
    except Exception:
        if prompt_written and current.read_text() == prompt:
            atomic_text(current, state["current_prompt"])
        raise
    return {**proposed, "applied": True, "backup": str(backup)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage-root", type=Path, required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--routine-id", required=True)
    parser.add_argument("--recipient", required=True)
    parser.add_argument("--prompt", type=Path, required=True)
    parser.add_argument("--routine", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--service-stopped", action="store_true")
    parser.add_argument("--expected-state", default="")
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    print(json.dumps(configure(args.storage_root, args.user, args.routine_id, args.prompt.read_text(),
        args.routine.read_text(), args.recipient, apply=args.apply, expected_state=args.expected_state,
        backup=args.backup, service_stopped=args.service_stopped), indent=2))


if __name__ == "__main__":
    main()
