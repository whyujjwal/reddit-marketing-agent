"""Local SQLite store: campaigns, the draft queue, an activity log, and seen-mention tracking."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    product_name TEXT NOT NULL,
    product_url TEXT,
    description TEXT NOT NULL,
    audience TEXT,
    keywords TEXT NOT NULL DEFAULT '[]',
    competitors TEXT NOT NULL DEFAULT '[]',
    subreddits TEXT NOT NULL DEFAULT '[]',
    tone TEXT,
    disclosure TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER REFERENCES campaigns(id),
    kind TEXT NOT NULL CHECK (kind IN ('comment', 'post')),
    subreddit TEXT NOT NULL,
    parent_fullname TEXT,
    parent_title TEXT,
    parent_permalink TEXT,
    title TEXT,
    body TEXT,
    url TEXT,
    flair_id TEXT,
    rationale TEXT,
    promotional INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'draft',
    checks TEXT NOT NULL DEFAULT '[]',
    error TEXT,
    reddit_fullname TEXT,
    permalink TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    approved_at REAL,
    published_at REAL
);
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER REFERENCES drafts(id),
    campaign_id INTEGER REFERENCES campaigns(id),
    kind TEXT NOT NULL,
    subreddit TEXT NOT NULL,
    reddit_fullname TEXT,
    permalink TEXT,
    body_hash TEXT,
    promotional INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    score INTEGER,
    num_comments INTEGER,
    removed INTEGER NOT NULL DEFAULT 0,
    checked_at REAL
);
CREATE TABLE IF NOT EXISTS seen (
    watch_key TEXT NOT NULL,
    fullname TEXT NOT NULL,
    first_seen_at REAL NOT NULL,
    PRIMARY KEY (watch_key, fullname)
);
"""

JSON_FIELDS = {"keywords", "competitors", "subreddits", "checks"}
DRAFT_STATUSES = {"draft", "approved", "published", "discarded", "failed"}


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    for key in JSON_FIELDS & out.keys():
        out[key] = json.loads(out[key] or "[]")
    return out


class Store:
    def __init__(self, path: Path | str, clock=time.time) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def close(self) -> None:
        self._conn.close()

    # -- campaigns ------------------------------------------------------------------------------
    def upsert_campaign(self, **fields: Any) -> dict[str, Any]:
        now = self._clock()
        for key in ("keywords", "competitors", "subreddits"):
            fields[key] = json.dumps(list(fields.get(key) or []))
        with self._tx() as db:
            existing = db.execute("SELECT id FROM campaigns WHERE name = ?", (fields["name"],)).fetchone()
            cols = [
                "product_name", "product_url", "description", "audience", "keywords",
                "competitors", "subreddits", "tone", "disclosure",
            ]  # fmt: skip
            if existing:
                sets = ", ".join(f"{c} = ?" for c in cols)
                db.execute(
                    f"UPDATE campaigns SET {sets}, updated_at = ? WHERE id = ?",
                    [fields.get(c) for c in cols] + [now, existing["id"]],
                )
                cid = existing["id"]
            else:
                cur = db.execute(
                    f"INSERT INTO campaigns (name, {', '.join(cols)}, created_at, updated_at) "
                    f"VALUES ({', '.join('?' * (len(cols) + 3))})",
                    [fields["name"]] + [fields.get(c) for c in cols] + [now, now],
                )
                cid = cur.lastrowid
        return self.get_campaign(cid)  # type: ignore[return-value]

    def get_campaign(self, ref: int | str) -> dict[str, Any] | None:
        with self._lock:
            if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
                row = self._conn.execute("SELECT * FROM campaigns WHERE id = ?", (int(ref),))
            else:
                row = self._conn.execute("SELECT * FROM campaigns WHERE name = ?", (ref,))
            return _row(row.fetchone())

    def list_campaigns(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM campaigns ORDER BY updated_at DESC")
            return [_row(r) for r in rows.fetchall()]  # type: ignore[misc]

    # -- drafts ---------------------------------------------------------------------------------
    def add_draft(self, **fields: Any) -> dict[str, Any]:
        now = self._clock()
        fields["checks"] = json.dumps(fields.get("checks") or [])
        fields["promotional"] = int(bool(fields.get("promotional")))
        cols = list(fields.keys())
        with self._tx() as db:
            cur = db.execute(
                f"INSERT INTO drafts ({', '.join(cols)}, created_at, updated_at) "
                f"VALUES ({', '.join('?' * (len(cols) + 2))})",
                [fields[c] for c in cols] + [now, now],
            )
            draft_id = cur.lastrowid
        return self.get_draft(draft_id)  # type: ignore[arg-type,return-value]

    def get_draft(self, draft_id: int) -> dict[str, Any] | None:
        with self._lock:
            return _row(self._conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone())

    def update_draft(self, draft_id: int, **fields: Any) -> dict[str, Any]:
        if "status" in fields and fields["status"] not in DRAFT_STATUSES:
            raise ValueError(f"Unknown draft status {fields['status']!r}")
        if "checks" in fields:
            fields["checks"] = json.dumps(fields["checks"])
        if "promotional" in fields:
            fields["promotional"] = int(bool(fields["promotional"]))
        fields["updated_at"] = self._clock()
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._tx() as db:
            db.execute(f"UPDATE drafts SET {sets} WHERE id = ?", [*fields.values(), draft_id])
        draft = self.get_draft(draft_id)
        if draft is None:
            raise KeyError(f"No draft with id {draft_id}")
        return draft

    def list_drafts(
        self, status: str | None = None, campaign_id: int | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM drafts WHERE 1=1", []
        if status:
            sql += " AND status = ?"
            args.append(status)
        if campaign_id is not None:
            sql += " AND campaign_id = ?"
            args.append(campaign_id)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            return [_row(r) for r in self._conn.execute(sql, args).fetchall()]  # type: ignore[misc]

    # -- activity -------------------------------------------------------------------------------
    def log_activity(self, **fields: Any) -> int:
        fields.setdefault("created_at", self._clock())
        fields["promotional"] = int(bool(fields.get("promotional")))
        cols = list(fields.keys())
        with self._tx() as db:
            cur = db.execute(
                f"INSERT INTO activity ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                [fields[c] for c in cols],
            )
            return int(cur.lastrowid or 0)

    def activity_since(self, since: float, campaign_id: int | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM activity WHERE created_at >= ?", [since]
        if campaign_id is not None:
            sql += " AND campaign_id = ?"
            args.append(campaign_id)
        sql += " ORDER BY created_at DESC"
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def update_activity_metrics(
        self, fullname: str, score: int | None, num_comments: int | None, removed: bool
    ) -> None:
        with self._tx() as db:
            db.execute(
                "UPDATE activity SET score = ?, num_comments = ?, removed = ?, checked_at = ? "
                "WHERE reddit_fullname = ?",
                (score, num_comments, int(removed), self._clock(), fullname),
            )

    # -- seen mentions --------------------------------------------------------------------------
    def filter_unseen(self, watch_key: str, fullnames: list[str]) -> set[str]:
        """Return the fullnames not seen before for ``watch_key`` and mark them all as seen."""
        now = self._clock()
        with self._tx() as db:
            seen = {
                r["fullname"]
                for r in db.execute("SELECT fullname FROM seen WHERE watch_key = ?", (watch_key,)).fetchall()
            }
            new = [f for f in fullnames if f not in seen]
            db.executemany(
                "INSERT OR IGNORE INTO seen (watch_key, fullname, first_seen_at) VALUES (?, ?, ?)",
                [(watch_key, f, now) for f in new],
            )
        return set(new)
