"""SQLite storage. Models are stored as JSON blobs plus a few indexed columns.

A single `Database` object is shared by the monitor and the web app. SQLite
calls are quick, so the async code calls these methods directly.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from .models import (
    DEAL_STATUSES,
    DealView,
    Evaluation,
    Listing,
    RunSummary,
    utcnow,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    ad_id        TEXT PRIMARY KEY,
    search_name  TEXT NOT NULL DEFAULT '',
    title        TEXT NOT NULL DEFAULT '',
    price        REAL,
    url          TEXT NOT NULL DEFAULT '',
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    data         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_listings_first_seen ON listings(first_seen);

CREATE TABLE IF NOT EXISTS evaluations (
    ad_id        TEXT PRIMARY KEY REFERENCES listings(ad_id) ON DELETE CASCADE,
    score        REAL NOT NULL DEFAULT 0,
    verdict      TEXT NOT NULL DEFAULT 'skip',
    purpose      TEXT NOT NULL DEFAULT 'resale',
    profit       REAL,
    evaluated_at TEXT NOT NULL,
    data         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evaluations_score ON evaluations(score);

CREATE TABLE IF NOT EXISTS deal_state (
    ad_id        TEXT PRIMARY KEY,
    status       TEXT NOT NULL DEFAULT 'new',
    note         TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    ad_id        TEXT NOT NULL,
    channel      TEXT NOT NULL,
    sent_at      TEXT NOT NULL,
    PRIMARY KEY (ad_id, channel)
);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    data         TEXT NOT NULL
);
"""

_SORTS = {
    "score": "COALESCE(e.score, -1) DESC, l.first_seen DESC",
    "newest": "l.first_seen DESC",
    "profit": "COALESCE(e.profit, -1e9) DESC, l.first_seen DESC",
    "price": "COALESCE(l.price, 1e9) ASC, l.first_seen DESC",
}


def _ts(dt: datetime) -> str:
    return dt.isoformat()


class Database:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ------------------------------------------------------------------ listings
    def has_listing(self, ad_id: str) -> bool:
        return bool(self._query("SELECT 1 FROM listings WHERE ad_id = ?", (ad_id,)))

    def upsert_listing(self, listing: Listing) -> bool:
        """Insert or update a listing. Returns True if it was new."""
        now = _ts(utcnow())
        is_new = not self.has_listing(listing.ad_id)
        if is_new:
            self._execute(
                "INSERT INTO listings (ad_id, search_name, title, price, url, first_seen, last_seen, data)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    listing.ad_id,
                    listing.search_name,
                    listing.title,
                    listing.price,
                    listing.url,
                    _ts(listing.first_seen),
                    now,
                    listing.model_dump_json(),
                ),
            )
        else:
            existing = self.get_listing(listing.ad_id)
            if existing is not None:
                # keep original discovery time and search, never downgrade detail info
                listing = listing.model_copy(
                    update={
                        "first_seen": existing.first_seen,
                        "search_name": existing.search_name or listing.search_name,
                    }
                )
                if existing.detail_loaded and not listing.detail_loaded:
                    listing = existing.model_copy(
                        update={
                            "price": listing.price,
                            "price_text": listing.price_text or existing.price_text,
                            "negotiable": listing.negotiable,
                            "is_free": listing.is_free,
                        }
                    )
            self._execute(
                "UPDATE listings SET title = ?, price = ?, url = ?, last_seen = ?, data = ?,"
                " search_name = ? WHERE ad_id = ?",
                (
                    listing.title,
                    listing.price,
                    listing.url,
                    now,
                    listing.model_dump_json(),
                    listing.search_name,
                    listing.ad_id,
                ),
            )
        return is_new

    def get_listing(self, ad_id: str) -> Listing | None:
        rows = self._query("SELECT data FROM listings WHERE ad_id = ?", (ad_id,))
        return Listing.model_validate_json(rows[0]["data"]) if rows else None

    def iter_listings(self) -> Iterator[Listing]:
        for row in self._query("SELECT data FROM listings ORDER BY first_seen DESC"):
            yield Listing.model_validate_json(row["data"])

    # --------------------------------------------------------------- evaluations
    def save_evaluation(self, ev: Evaluation) -> None:
        self._execute(
            "INSERT INTO evaluations (ad_id, score, verdict, purpose, profit, evaluated_at, data)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(ad_id) DO UPDATE SET score = excluded.score, verdict = excluded.verdict,"
            " purpose = excluded.purpose, profit = excluded.profit,"
            " evaluated_at = excluded.evaluated_at, data = excluded.data",
            (
                ev.ad_id,
                ev.score,
                ev.verdict,
                ev.purpose,
                ev.expected_profit,
                _ts(ev.evaluated_at),
                ev.model_dump_json(),
            ),
        )

    def get_evaluation(self, ad_id: str) -> Evaluation | None:
        rows = self._query("SELECT data FROM evaluations WHERE ad_id = ?", (ad_id,))
        return Evaluation.model_validate_json(rows[0]["data"]) if rows else None

    # ------------------------------------------------------------- user status
    def set_status(self, ad_id: str, status: str, note: str | None = None) -> None:
        if status not in DEAL_STATUSES:
            raise ValueError(f"unknown status {status!r}; expected one of {DEAL_STATUSES}")
        current = self._query("SELECT note FROM deal_state WHERE ad_id = ?", (ad_id,))
        keep_note = current[0]["note"] if current else ""
        self._execute(
            "INSERT INTO deal_state (ad_id, status, note, updated_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(ad_id) DO UPDATE SET status = excluded.status, note = excluded.note,"
            " updated_at = excluded.updated_at",
            (ad_id, status, keep_note if note is None else note, _ts(utcnow())),
        )

    # ------------------------------------------------------------ notifications
    def mark_notified(self, ad_id: str, channel: str) -> None:
        self._execute(
            "INSERT OR REPLACE INTO notifications (ad_id, channel, sent_at) VALUES (?, ?, ?)",
            (ad_id, channel, _ts(utcnow())),
        )

    def was_notified(self, ad_id: str, channel: str | None = None) -> bool:
        if channel is None:
            rows = self._query("SELECT 1 FROM notifications WHERE ad_id = ?", (ad_id,))
        else:
            rows = self._query(
                "SELECT 1 FROM notifications WHERE ad_id = ? AND channel = ?", (ad_id, channel)
            )
        return bool(rows)

    # -------------------------------------------------------------------- deals
    def _deal_from_row(self, row: sqlite3.Row) -> DealView:
        return DealView(
            listing=Listing.model_validate_json(row["l_data"]),
            evaluation=Evaluation.model_validate_json(row["e_data"]) if row["e_data"] else None,
            status=row["status"] or "new",
            note=row["note"] or "",
            notified=bool(row["notified"]),
        )

    _DEAL_SELECT = (
        "SELECT l.data AS l_data, e.data AS e_data, s.status AS status, s.note AS note,"
        " EXISTS(SELECT 1 FROM notifications n WHERE n.ad_id = l.ad_id) AS notified"
        " FROM listings l"
        " LEFT JOIN evaluations e ON e.ad_id = l.ad_id"
        " LEFT JOIN deal_state s ON s.ad_id = l.ad_id"
    )

    def get_deal(self, ad_id: str) -> DealView | None:
        rows = self._query(self._DEAL_SELECT + " WHERE l.ad_id = ?", (ad_id,))
        return self._deal_from_row(rows[0]) if rows else None

    def _deal_filters(
        self,
        verdict: str | list[str] | None,
        min_score: float | None,
        search_name: str | None,
        status: str | list[str] | None,
        purpose: str | None,
        q: str | None,
        since: datetime | None,
        include_ignored: bool,
        source: str | None = None,
    ) -> tuple[str, list[Any]]:
        where: list[str] = []
        params: list[Any] = []
        if verdict:
            verdicts = [verdict] if isinstance(verdict, str) else list(verdict)
            where.append(f"e.verdict IN ({','.join('?' * len(verdicts))})")
            params += verdicts
        if min_score is not None:
            where.append("e.score >= ?")
            params.append(min_score)
        if search_name:
            where.append("l.search_name = ?")
            params.append(search_name)
        if status:
            statuses = [status] if isinstance(status, str) else list(status)
            where.append(f"COALESCE(s.status, 'new') IN ({','.join('?' * len(statuses))})")
            params += statuses
        elif not include_ignored:
            where.append("COALESCE(s.status, 'new') != 'ignored'")
        if purpose:
            where.append("e.purpose = ?")
            params.append(purpose)
        if q:
            where.append("LOWER(l.title) LIKE ?")
            params.append(f"%{q.lower()}%")
        if since is not None:
            where.append("l.first_seen >= ?")
            params.append(_ts(since))
        if source:
            where.append("COALESCE(json_extract(l.data, '$.source'), 'kleinanzeigen') = ?")
            params.append(source)
        return (" WHERE " + " AND ".join(where)) if where else "", params

    def list_deals(
        self,
        verdict: str | list[str] | None = None,
        min_score: float | None = None,
        search_name: str | None = None,
        status: str | list[str] | None = None,
        purpose: str | None = None,
        q: str | None = None,
        since: datetime | None = None,
        include_ignored: bool = False,
        source: str | None = None,
        sort: str = "score",
        limit: int = 100,
        offset: int = 0,
    ) -> list[DealView]:
        """Listings joined with evaluation + user status. Ignored ones are hidden
        unless `include_ignored` or an explicit `status` filter is given."""
        where, params = self._deal_filters(
            verdict, min_score, search_name, status, purpose, q, since, include_ignored, source
        )
        order = _SORTS.get(sort, _SORTS["score"])
        rows = self._query(
            self._DEAL_SELECT + where + f" ORDER BY {order} LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        return [self._deal_from_row(r) for r in rows]

    def count_deals(self, **filters: Any) -> int:
        where, params = self._deal_filters(
            filters.get("verdict"),
            filters.get("min_score"),
            filters.get("search_name"),
            filters.get("status"),
            filters.get("purpose"),
            filters.get("q"),
            filters.get("since"),
            filters.get("include_ignored", False),
            filters.get("source"),
        )
        rows = self._query(
            "SELECT COUNT(*) AS c FROM listings l"
            " LEFT JOIN evaluations e ON e.ad_id = l.ad_id"
            " LEFT JOIN deal_state s ON s.ad_id = l.ad_id" + where,
            params,
        )
        return int(rows[0]["c"])

    def search_names(self) -> list[str]:
        rows = self._query(
            "SELECT DISTINCT search_name FROM listings WHERE search_name != '' ORDER BY search_name"
        )
        return [r["search_name"] for r in rows]

    # --------------------------------------------------------------------- runs
    def start_run(self) -> RunSummary:
        summary = RunSummary()
        cur = self._execute(
            "INSERT INTO runs (started_at, data) VALUES (?, ?)",
            (_ts(summary.started_at), summary.model_dump_json()),
        )
        summary.id = cur.lastrowid
        self._execute("UPDATE runs SET data = ? WHERE id = ?", (summary.model_dump_json(), summary.id))
        return summary

    def finish_run(self, summary: RunSummary) -> None:
        if summary.finished_at is None:
            summary.finished_at = utcnow()
        self._execute(
            "UPDATE runs SET finished_at = ?, data = ? WHERE id = ?",
            (_ts(summary.finished_at), summary.model_dump_json(), summary.id),
        )

    def list_runs(self, limit: int = 20) -> list[RunSummary]:
        rows = self._query("SELECT data FROM runs ORDER BY id DESC LIMIT ?", (limit,))
        return [RunSummary.model_validate_json(r["data"]) for r in rows]

    # -------------------------------------------------------------------- stats
    def stats(self) -> dict[str, Any]:
        day_ago = _ts(utcnow() - timedelta(days=1))
        one = lambda sql, p=(): self._query(sql, p)[0][0]  # noqa: E731
        return {
            "listings_total": one("SELECT COUNT(*) FROM listings"),
            "listings_24h": one("SELECT COUNT(*) FROM listings WHERE first_seen >= ?", (day_ago,)),
            "evaluated_total": one("SELECT COUNT(*) FROM evaluations"),
            "buy_total": one("SELECT COUNT(*) FROM evaluations WHERE verdict = 'buy'"),
            "maybe_total": one("SELECT COUNT(*) FROM evaluations WHERE verdict = 'maybe'"),
            "buy_24h": one(
                "SELECT COUNT(*) FROM evaluations e JOIN listings l ON l.ad_id = e.ad_id"
                " WHERE e.verdict = 'buy' AND l.first_seen >= ?",
                (day_ago,),
            ),
            "potential_profit": one(
                "SELECT COALESCE(SUM(e.profit), 0) FROM evaluations e"
                " LEFT JOIN deal_state s ON s.ad_id = e.ad_id"
                " WHERE e.verdict = 'buy' AND e.purpose = 'resale'"
                " AND COALESCE(s.status, 'new') != 'ignored' AND e.profit > 0"
            ),
            "bought_total": one("SELECT COUNT(*) FROM deal_state WHERE status = 'bought'"),
            "notified_total": one("SELECT COUNT(DISTINCT ad_id) FROM notifications"),
        }
