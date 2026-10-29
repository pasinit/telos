"""Seed the nightly memory-reconcile agent + routine for Telos users. Dry run unless --apply.

    python scripts/seed_memory_reconcile.py --storage-root /data --user <id> [--user <id> ...] [--apply] [--enable]
    python scripts/seed_memory_reconcile.py --print-rules > memory_reconcile_rules.md

Per user it creates the ``memory-steward`` agent (judge prompt, the three
``memory_reconcile_*`` tools) and a daily routine, disabled unless ``--enable``.
The routine runs only on Cardea; enable it together with
``TELOS_CARDEA_ROUTINES='{"<routine id>": "live"}'`` (or "shadow" for a preview
run). Re-running is safe: existing agents and routines are left as they are.
``--print-rules`` prints the judging rules for Recordatio's local harness
(``agentmem reconcile --judge-prompt``).
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telos.core.persistence import Database  # noqa: E402
from telos.core.repos.registry import RegistryRepo  # noqa: E402
from telos.core.repos.routines import RoutineRepo  # noqa: E402
from telos.services.memory_reconcile import JUDGE_RULES, seed  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--storage-root", type=Path, help="Telos storage root (holds registry.db and routines.db)")
    parser.add_argument("--user", action="append", dest="users", default=[], help="Telos user id (repeatable)")
    parser.add_argument("--apply", action="store_true", help="write (default: report what would be created)")
    parser.add_argument("--enable", action="store_true", help="create the routine enabled")
    parser.add_argument("--print-rules", action="store_true", help="print the judging rules and exit")
    args = parser.parse_args(argv)
    if args.print_rules:
        print(JUDGE_RULES)
        return 0
    if not args.storage_root or not args.users:
        parser.error("--storage-root and at least one --user are required")
    registry = RegistryRepo(Database(args.storage_root / "registry.db"))
    routines = RoutineRepo(Database(args.storage_root / "routines.db"))
    for user in args.users:
        result = seed(registry, routines, user, apply=args.apply, enable=args.enable)
        verb = "created" if args.apply else "would create"
        routine = result.routine.id if result.routine else "(new)"
        print(f"{user}: agent {verb if result.agent_created else 'exists'}; "
              f"routine {routine} {verb if result.routine_created else 'exists'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
