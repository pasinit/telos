"""Per-agent property assessments; append-only evidence independent of emails."""

import json
from datetime import UTC, datetime

from telos.core.persistence import Database


class PropertyCandidateRepo:
    @staticmethod
    def exists(db: Database) -> bool:
        with db.connect() as conn:
            return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='property_assessments'").fetchone() is not None

    def __init__(self, db: Database) -> None:
        self._db = db
        with db.connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS property_assessments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL, recorded_at TEXT NOT NULL, payload_json TEXT NOT NULL
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS property_assessments_url ON property_assessments(url, id)")

    def record(self, url: str, assessment: dict, *, now: datetime | None = None) -> dict:
        timestamp = (now or datetime.now(UTC)).isoformat()
        with self._db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                "SELECT payload_json FROM property_assessments WHERE url=? ORDER BY id DESC LIMIT 1", (url,),
            ).fetchone()
            old = json.loads(previous[0]) if previous else {}
            if old.get("disposition") == "applied" and assessment["disposition"] != "applied":
                raise ValueError("Applied properties remain excluded; an explicit user correction is required")
            payload = {**assessment, "url": url, "recorded_at": timestamp,
                       "first_recorded_at": old.get("first_recorded_at", timestamp),
                       "first_seen": old.get("first_seen") or assessment.get("first_seen_at")}
            payload.pop("verification_deadline", None)
            if assessment["disposition"] in ("needs_verification", "dropped"):
                since = old.get("unresolved_since") or assessment.get("unresolved_since")
                payload["unresolved_since"] = since
            conn.execute("INSERT INTO property_assessments(url,recorded_at,payload_json) VALUES (?,?,?)",
                         (url, timestamp, json.dumps(payload, ensure_ascii=False)))
        return payload

    def page(self, *, offset: int = 0, limit: int = 20) -> dict:
        offset, limit = max(0, offset), min(max(1, limit), 20)
        with self._db.connect() as conn:
            total = conn.execute("SELECT COUNT(DISTINCT url) FROM property_assessments").fetchone()[0]
            rows = conn.execute("""SELECT payload_json FROM property_assessments
                WHERE id IN (SELECT MAX(id) FROM property_assessments GROUP BY url)
                ORDER BY id DESC LIMIT ? OFFSET ?""", (limit, offset)).fetchall()
        records = [json.loads(row[0]) for row in rows]
        for record in records:
            record.pop("verification_deadline", None)
        return {"total": total, "offset": offset, "has_more": offset + len(rows) < total,
                "records": records}

    def history(self, url: str, *, offset: int = 0, limit: int = 20) -> list[dict]:
        with self._db.connect() as conn:
            rows = conn.execute("""SELECT payload_json FROM property_assessments WHERE url=?
                ORDER BY id DESC LIMIT ? OFFSET ?""", (url, min(max(1, limit), 20), max(0, offset))).fetchall()
        return [json.loads(row[0]) for row in rows]

    def summary(self, *, limit: int = 10) -> dict:
        page = self.page(limit=limit)
        keys = ("url", "address", "disposition", "first_seen", "first_recorded_at", "recorded_at", "unresolved_since", "next_action")
        page["records"] = [
            {**{key: row[key] for key in keys if key in row},
             "criterion_statuses": {key: value["status"] for key, value in row.get("criteria", {}).items()}}
            for row in page["records"]
        ]
        return page
