"""Bounded execution evidence, stored beside its owning agent's turns."""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from telos.core.persistence import Database

_SECRET = re.compile(r"password|secret|authorization|api[_-]?key|access[_-]?token|refresh[_-]?token", re.I)
_MAX_JSON = 160_000


def _safe(value: Any) -> Any:
    if isinstance(value, dict):
        if value.get("type") == "image":
            return {"type": "image", "mimeType": value.get("mimeType"), "data": "[image omitted]"}
        return {k: "[redacted]" if _SECRET.search(k) else _safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_safe(v) for v in value]
    if isinstance(value, str):
        if value.startswith("data:image/"):
            return "[image omitted]"
        return value
    return value


class ExecutionEventRepo:
    def __init__(self, db: Database):
        self._db = db
        with db.connect() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS execution_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, turn_id TEXT NOT NULL,
                timestamp TEXT NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL
            )''')
            conn.execute("CREATE INDEX IF NOT EXISTS execution_events_turn ON execution_events(turn_id, id)")

    def append(self, turn_id: str, kind: str, payload: dict) -> None:
        raw = json.dumps(_safe(payload), ensure_ascii=False, default=str)
        if len(raw) > _MAX_JSON:
            raw = json.dumps({"truncated": True, "original_chars": len(raw), "preview": raw[:_MAX_JSON]})
        with self._db.connect() as conn:
            # Reject an accidental cross-turn/cross-agent association.
            if not conn.execute("SELECT 1 FROM turns WHERE turn_id=?", (turn_id,)).fetchone():
                raise ValueError("Execution evidence requires an existing turn in this agent's history")
            conn.execute("INSERT INTO execution_events(turn_id,timestamp,kind,payload_json) VALUES (?,?,?,?)",
                         (turn_id, datetime.now(UTC).isoformat(), kind, raw))

    def list(self, turn_id: str, *, offset: int = 0, limit: int = 10) -> list[dict]:
        with self._db.connect() as conn:
            rows = conn.execute("SELECT id,timestamp,kind,payload_json FROM execution_events WHERE turn_id=? ORDER BY id LIMIT ? OFFSET ?",
                                (turn_id, min(max(limit, 1), 20), max(offset, 0))).fetchall()
        return [{"id": r[0], "timestamp": r[1], "kind": r[2], "payload": json.loads(r[3])} for r in rows]

    def page(self, turn_id: str, *, offset: int = 0, limit: int = 5) -> dict:
        with self._db.connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM execution_events WHERE turn_id=?", (turn_id,)).fetchone()[0]
        return {"available": total > 0, "total": total, "offset": max(offset, 0),
                "events": self.list(turn_id, offset=offset, limit=limit)}

    def recent_email_deliveries(self, recipient: str, *, limit: int = 31) -> list[dict]:
        """Return confirmed sends, not drafts or an assistant's claim of sending.

        Tool sends count even when the surrounding turn subsequently failed.
        Scheduler failure notices are not successful recap deliveries.
        """
        with self._db.connect() as conn:
            rows = conn.execute("""
                SELECT e.timestamp, e.payload_json, e.kind
                FROM execution_events e
                WHERE (
                    e.kind = 'tool_call'
                    AND json_extract(e.payload_json, '$.name') = 'send_email'
                    AND lower(json_extract(e.payload_json, '$.input.to')) = lower(?)
                    AND EXISTS (
                        SELECT 1 FROM execution_events r
                        WHERE r.turn_id = e.turn_id AND r.id > e.id
                          AND r.kind = 'tool_result'
                          AND json_extract(r.payload_json, '$.call_id') = json_extract(e.payload_json, '$.call_id')
                          AND json_type(r.payload_json, '$.is_error') = 'false'
                    )
                ) OR (
                    e.kind = 'email_delivery'
                    AND lower(json_extract(e.payload_json, '$.to')) = lower(?)
                    AND json_extract(e.payload_json, '$.status') = 'accepted_by_smtp'
                    AND NOT EXISTS (
                        SELECT 1 FROM execution_events f
                        WHERE f.turn_id = e.turn_id AND f.kind = 'turn_failed' AND f.id < e.id
                    )
                )
                ORDER BY e.id DESC LIMIT ?
            """, (recipient, recipient, min(max(1, limit), 100))).fetchall()
        deliveries = []
        for timestamp, raw, kind in rows:
            payload = json.loads(raw)
            email = payload['input'] if kind == 'tool_call' else payload
            deliveries.append({"timestamp": timestamp, "subject": email.get("subject", ""),
                               "body": email.get("body", "")})
        return deliveries
