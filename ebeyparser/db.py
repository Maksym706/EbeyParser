"""SQLite storage. Models are stored as JSON blobs plus a few indexed columns.

A single `Database` object is shared by the monitor and the web app. SQLite
calls are quick, so the async code calls these methods directly.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator

from .models import (
    DEAL_STATUSES,
    Comparable,
    DealView,
    Evaluation,
    Listing,
    RunSummary,
    utcnow,
)
from .pricing.text import history_anchor_words, normalize, price_point_words
from .timefmt import date_label

log = logging.getLogger(__name__)

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

-- Price history: every price we have seen (search results, comparables), ONE row per ad;
-- price and seen_at are refreshed whenever the ad shows up again.
CREATE TABLE IF NOT EXISTS price_points (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ad_id        TEXT NOT NULL UNIQUE,
    source       TEXT NOT NULL,
    product_key  TEXT NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    price        REAL NOT NULL,
    sold         INTEGER NOT NULL DEFAULT 0,
    url          TEXT NOT NULL DEFAULT '',
    seen_at      TEXT NOT NULL,  -- last time the price was seen (= last_seen; kept for the window)
    first_seen   TEXT,           -- first time this ad/price was seen (listing age)
    last_seen    TEXT
);
CREATE INDEX IF NOT EXISTS idx_price_points_key ON price_points(product_key, seen_at);
CREATE INDEX IF NOT EXISTS idx_price_points_seen ON price_points(seen_at);

-- Words (model numbers, product lines) of each price point, to find the same product
-- written differently ("Apple iPhone 13 128GB" ~ "iPhone13 128 GB") without a table scan.
CREATE TABLE IF NOT EXISTS price_point_words (
    word         TEXT NOT NULL,
    point_id     INTEGER NOT NULL REFERENCES price_points(id) ON DELETE CASCADE,
    PRIMARY KEY (word, point_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_price_point_words_point ON price_point_words(point_id);

-- Small persistent state: alert dedupe, per-search streaks, heartbeat, migration markers.
CREATE TABLE IF NOT EXISTS kv_state (
    key          TEXT PRIMARY KEY,
    value        TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL
);

-- Failed notification deliveries per channel (retried on later passes).
CREATE TABLE IF NOT EXISTS notify_attempts (
    ad_id        TEXT NOT NULL,
    channel      TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    first_at     TEXT NOT NULL,
    last_at      TEXT NOT NULL,
    last_error   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (ad_id, channel)
);

-- Deals held back by notifications.max_alerts_per_hour, sent later as one digest.
CREATE TABLE IF NOT EXISTS alert_queue (
    ad_id        TEXT PRIMARY KEY,
    queued_at    TEXT NOT NULL
);

-- AI scout (docs/design/AI_SCOUT.md): what the text model read in each ad (JSON of ai.triage.TriageItem).
CREATE TABLE IF NOT EXISTS scout_triage (
    ad_id        TEXT PRIMARY KEY REFERENCES listings(ad_id) ON DELETE CASCADE,
    triaged_at   TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT 'ai',  -- ai | script (the model's answer was unusable)
    kind         TEXT NOT NULL DEFAULT '',
    interest     INTEGER NOT NULL DEFAULT 0,
    product      TEXT NOT NULL DEFAULT '',
    data         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scout_triage_at ON scout_triage(triaged_at);

-- Would-be deals waiting for the vision model (it runs on a PC that is sometimes off).
CREATE TABLE IF NOT EXISTS vision_queue (
    ad_id        TEXT PRIMARY KEY REFERENCES listings(ad_id) ON DELETE CASCADE,
    search_name  TEXT NOT NULL DEFAULT '',
    queued_at    TEXT NOT NULL
);

-- Searches that have run at least once (baseline_first_run: the first pass only learns prices).
CREATE TABLE IF NOT EXISTS search_state (
    name         TEXT PRIMARY KEY,
    first_run_at TEXT NOT NULL,
    last_run_at  TEXT NOT NULL,
    baseline_at  TEXT
);
"""

_KA_AD_RE = re.compile(r"/s-anzeige/[^/]+/(\d+)")
_EBAY_ITEM_RE = re.compile(r"/itm/(?:[^/?#]+/)?(\d{9,})")
_POINT_SOURCES = frozenset({"kleinanzeigen", "ebay", "ebay_sold", "reference", "other"})


def price_point_id(item: Listing | Comparable) -> str:
    """Stable id of a price point: the ad id when the URL has one (so an ad seen in search
    results and again as a comparable is stored once), else URL or title+price."""
    if isinstance(item, Listing):
        return item.ad_id
    url = item.url or ""
    if "ebay." in url:
        match = _EBAY_ITEM_RE.search(url)
        if match:
            return f"ebay-{match.group(1)}"
    else:
        match = _KA_AD_RE.search(url)
        if match:
            return match.group(1)
    if url:
        return url
    return f"{item.source}:{normalize(item.title)[:120]}:{item.price:.2f}"

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
        self._migrate_schema()
        self._migrate_deal_state()
        self._migrate_projects()
        self._conn.commit()

    def _migrate_schema(self) -> None:
        """Columns added after a table first shipped (CREATE TABLE IF NOT EXISTS won't add them)."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(price_points)")}
        for col in ("first_seen", "last_seen"):
            if col not in cols:
                self._conn.execute(f"ALTER TABLE price_points ADD COLUMN {col} TEXT")
        self._conn.execute(
            "UPDATE price_points SET first_seen = COALESCE(first_seen, seen_at), last_seen = COALESCE(last_seen, seen_at)"
            " WHERE first_seen IS NULL OR last_seen IS NULL"
        )

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

    def pending_listings(
        self, search_name: str, *, since: datetime | None = None, limit: int = 100
    ) -> list[Listing]:
        """Listings of a search that were stored but never evaluated (deferred by a budget,
        interrupted run), oldest first."""
        sql = (
            "SELECT l.data FROM listings l LEFT JOIN evaluations e ON e.ad_id = l.ad_id"
            " WHERE e.ad_id IS NULL AND l.search_name = ?"
        )
        params: list[Any] = [search_name]
        if since is not None:
            sql += " AND l.first_seen >= ?"
            params.append(_ts(since))
        sql += " ORDER BY l.first_seen ASC LIMIT ?"
        params.append(max(0, int(limit)))
        return [Listing.model_validate_json(r["data"]) for r in self._query(sql, params)]

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

    def clear_evaluations(self) -> int:
        """Forget all verdicts (listings stay); the next run evaluates them again."""
        return self._execute("DELETE FROM evaluations").rowcount

    def delete_evaluation(self, ad_id: str) -> bool:
        """Forget one verdict; the listing becomes pending again."""
        return self._execute("DELETE FROM evaluations WHERE ad_id = ?", (ad_id,)).rowcount > 0

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

    # ------------------------------------------------------------ price history
    def add_price_points(
        self, product_key: str, items: Iterable[Listing | Comparable], *, seen_at: datetime | None = None
    ) -> int:
        """Remember prices of `items` under one product key (e.g. the comparables of a query).
        Returns how many were stored; items without a positive price are ignored."""
        return self.record_price_points(((product_key, item) for item in items), seen_at=seen_at)

    def record_price_points(
        self, rows: Iterable[tuple[str, Listing | Comparable]], *, seen_at: datetime | None = None
    ) -> int:
        """Like add_price_points, with a product key per item; one transaction. One row per ad:
        seeing it again updates its price, key and seen_at. Listings count as asking prices;
        a comparable keeps its source and `sold` flag."""
        when = _ts(seen_at or utcnow())
        prepared: list[tuple[Any, ...]] = []
        for key, item in rows:
            key = (key or "").strip()  # stored verbatim: identity keys look like "iphone|13||128gb"
            price = item.price
            if not key or price is None or not math.isfinite(price) or price <= 0:
                continue
            if isinstance(item, Listing):
                source, sold = item.source, False
            else:
                source = item.source if item.source in _POINT_SOURCES else "other"
                sold = bool(item.sold or item.source == "ebay_sold")
            words = price_point_words(item.title, key)
            prepared.append((price_point_id(item), source, key, item.title, float(price), int(sold),
                             item.url or "", when, words))
        if not prepared:
            return 0
        with self._lock:
            cur = self._conn.cursor()
            try:
                for ad_id, source, key, title, price, sold, url, ts, words in prepared:
                    cur.execute(
                        "INSERT INTO price_points (ad_id, source, product_key, title, price, sold, url, seen_at,"
                        " first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                        " ON CONFLICT(ad_id) DO UPDATE SET source = excluded.source,"
                        " product_key = excluded.product_key, title = excluded.title, price = excluded.price,"
                        " sold = excluded.sold, url = excluded.url, seen_at = excluded.seen_at,"
                        " last_seen = excluded.last_seen,"
                        " first_seen = COALESCE(price_points.first_seen, excluded.first_seen)",
                        (ad_id, source, key, title, price, sold, url, ts, ts, ts),
                    )
                    point_id = cur.execute(
                        "SELECT id FROM price_points WHERE ad_id = ?", (ad_id,)
                    ).fetchone()[0]
                    cur.execute("DELETE FROM price_point_words WHERE point_id = ?", (point_id,))
                    cur.executemany(
                        "INSERT OR IGNORE INTO price_point_words (word, point_id) VALUES (?, ?)",
                        [(w, point_id) for w in words],
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return len(prepared)

    def price_history(
        self,
        product_key: str,
        since: datetime | None = None,
        *,
        exclude_ad_id: str | None = None,
        limit: int = 400,
    ) -> list[Comparable]:
        """Remembered prices that may be `product_key`, newest first: all points sharing its
        anchor words (model number + product line). They are candidates — filter them with
        pricing.estimator.comparable_is_relevant (estimate_from_history does)."""
        return [c for c, _ in self.price_history_dated(product_key, since, exclude_ad_id=exclude_ad_id, limit=limit)]

    def price_history_dated(
        self,
        product_key: str,
        since: datetime | None = None,
        *,
        exclude_ad_id: str | None = None,
        limit: int = 400,
    ) -> list[tuple[Comparable, datetime]]:
        """Like price_history, with the time each price was last seen (for time weighting)."""
        key = " ".join(normalize(product_key).split())
        if not key:
            return []
        anchors = history_anchor_words(key)
        params: list[Any] = []
        if anchors:
            sql = ("SELECT p.* FROM price_point_words w JOIN price_points p ON p.id = w.point_id"
                   " WHERE w.word = ?")
            params.append(anchors[0])
            for word in anchors[1:]:
                sql += (" AND EXISTS (SELECT 1 FROM price_point_words w2"
                        " WHERE w2.word = ? AND w2.point_id = p.id)")
                params.append(word)
        else:
            sql = "SELECT p.* FROM price_points p WHERE p.product_key = ?"
            params.append(key)
        if since is not None:
            sql += " AND p.seen_at >= ?"
            params.append(_ts(since))
        if exclude_ad_id:
            sql += " AND p.ad_id != ?"
            params.append(exclude_ad_id)
        sql += " ORDER BY p.seen_at DESC LIMIT ?"
        params.append(max(0, int(limit)))
        out: list[tuple[Comparable, datetime]] = []
        for r in self._query(sql, params):
            seen = datetime.fromisoformat(r["seen_at"])
            out.append((
                Comparable(title=r["title"], price=r["price"], url=r["url"], source=r["source"],
                           sold=bool(r["sold"]), date_text=date_label(seen)),
                seen,
            ))
        return out

    def price_history_prefix(
        self,
        key: str,
        since: datetime | None = None,
        *,
        exclude_ad_id: str | None = None,
        limit: int = 400,
    ) -> list[tuple[Comparable, datetime]]:
        """Prices stored under `key` or any finer key below it ("iphone|13" also returns
        "iphone|13|pro|256gb" — filter with identity.comparable_matches), newest first.
        Prefixed kinds ("bundle:iphone|13") only match a prefixed `key`."""
        key = (key or "").strip()
        if not key:
            return []
        # '}' sorts right after '|': the range is exactly the keys starting with "key|"
        sql = ("SELECT * FROM price_points WHERE (product_key = ? OR (product_key >= ? AND product_key < ?))")
        params: list[Any] = [key, key + "|", key + "}"]
        if since is not None:
            sql += " AND seen_at >= ?"
            params.append(_ts(since))
        if exclude_ad_id:
            sql += " AND ad_id != ?"
            params.append(exclude_ad_id)
        sql += " ORDER BY seen_at DESC LIMIT ?"
        params.append(max(0, int(limit)))
        out: list[tuple[Comparable, datetime]] = []
        for r in self._query(sql, params):
            seen = datetime.fromisoformat(r["seen_at"])
            out.append((
                Comparable(title=r["title"], price=r["price"], url=r["url"], source=r["source"],
                           sold=bool(r["sold"]), date_text=date_label(seen)),
                seen,
            ))
        return out

    def price_history_spans(
        self,
        key: str,
        since: datetime | None = None,
        *,
        exclude_ad_id: str | None = None,
        limit: int = 400,
    ) -> list[tuple[Comparable, datetime, datetime]]:
        """Like price_history_prefix, with (first_seen, last_seen) per price: how long an ad has
        been listed matters (a price asked for weeks doesn't sell)."""
        rows = self._price_rows(key, since, exclude_ad_id, limit)
        out: list[tuple[Comparable, datetime, datetime]] = []
        for r in rows:
            last = datetime.fromisoformat(r["last_seen"] or r["seen_at"])
            first = datetime.fromisoformat(r["first_seen"] or r["seen_at"])
            out.append((self._point_comparable(r, last), first, last))
        return out

    def _price_rows(self, key: str, since: datetime | None, exclude_ad_id: str | None, limit: int) -> list[sqlite3.Row]:
        key = (key or "").strip()
        if not key:
            return []
        sql = ("SELECT * FROM price_points WHERE (product_key = ? OR (product_key >= ? AND product_key < ?))")
        params: list[Any] = [key, key + "|", key + "}"]
        if since is not None:
            sql += " AND seen_at >= ?"
            params.append(_ts(since))
        if exclude_ad_id:
            sql += " AND ad_id != ?"
            params.append(exclude_ad_id)
        sql += " ORDER BY seen_at DESC LIMIT ?"
        params.append(max(0, int(limit)))
        return self._query(sql, params)

    def price_history_keyed(
        self,
        key: str,
        since: datetime | None = None,
        *,
        exclude_ad_id: str | None = None,
        limit: int = 400,
    ) -> list[tuple[Comparable, datetime, datetime, str]]:
        """Like price_history_spans, plus the key each price is stored under (the AI scout's
        "ai:…" keys are compared by their attributes, not by the title)."""
        rows = self._price_rows(key, since, exclude_ad_id, limit)
        out: list[tuple[Comparable, datetime, datetime, str]] = []
        for r in rows:
            last = datetime.fromisoformat(r["last_seen"] or r["seen_at"])
            first = datetime.fromisoformat(r["first_seen"] or r["seen_at"])
            out.append((self._point_comparable(r, last), first, last, r["product_key"]))
        return out

    @staticmethod
    def _point_comparable(r: sqlite3.Row, seen: datetime) -> Comparable:
        return Comparable(title=r["title"], price=r["price"], url=r["url"], source=r["source"],
                          sold=bool(r["sold"]), date_text=date_label(seen))

    def prune_price_points(self, days: float | None = None, *, before: datetime | None = None) -> int:
        """Delete price points not seen for `days` days (or since `before`). Returns the count."""
        if before is None:
            if days is None:
                raise ValueError("prune_price_points needs `days` or `before`")
            before = utcnow() - timedelta(days=days)
        return self._execute("DELETE FROM price_points WHERE seen_at < ?", (_ts(before),)).rowcount

    def count_price_points(self, product_key: str | None = None) -> int:
        if product_key is None:
            return int(self._query("SELECT COUNT(*) FROM price_points")[0][0])
        return int(self._query("SELECT COUNT(*) FROM price_points WHERE product_key = ?",
                               (product_key.strip(),))[0][0])

    # ------------------------------------------------------------------- AI scout
    def save_triage(self, items: Iterable[dict[str, Any]]) -> int:
        """Store the scout's reading of ads: dicts of ai.triage.TriageItem (mode="json");
        ads not in `listings` are skipped (the foreign key would refuse them)."""
        rows = []
        for item in items:
            ad_id = str(item.get("ad_id") or "")
            if not ad_id:
                continue
            rows.append((ad_id, str(item.get("triaged_at") or _ts(utcnow())), str(item.get("source") or "ai"),
                         str(item.get("kind") or ""), int(item.get("interest") or 0),
                         str(item.get("product") or "")[:200], json.dumps(item, ensure_ascii=False)))
        if not rows:
            return 0
        stored = 0
        with self._lock:
            for row in rows:
                cur = self._conn.execute(
                    "INSERT INTO scout_triage (ad_id, triaged_at, source, kind, interest, product, data)"
                    " SELECT ?, ?, ?, ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM listings WHERE ad_id = ?)"
                    " ON CONFLICT(ad_id) DO UPDATE SET triaged_at = excluded.triaged_at, source = excluded.source,"
                    " kind = excluded.kind, interest = excluded.interest, product = excluded.product,"
                    " data = excluded.data", (*row, row[0]))
                stored += cur.rowcount
            self._conn.commit()
        return stored

    def get_triage(self, ad_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Stored scout readings by ad id (JSON dicts)."""
        ids = list(dict.fromkeys(ad_ids))
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for r in self._query(f"SELECT ad_id, data FROM scout_triage WHERE ad_id IN ({','.join('?' * len(chunk))})",
                                 chunk):
                try:
                    out[r["ad_id"]] = json.loads(r["data"])
                except ValueError:
                    continue
        return out

    def scout_backlog(self, since: datetime, *, min_interest: int = 0, limit: int = 100) -> list[Listing]:
        """Ads the script dismissed (free checks / market data) since `since` that the scout has not
        read yet (or whose reading failed: source "script"), or read as interesting (>= min_interest)
        without a second look so far — the scout's "second look" when there is time left in a pass."""
        rows = self._query(
            "SELECT l.data FROM listings l JOIN evaluations e ON e.ad_id = l.ad_id"
            " LEFT JOIN scout_triage t ON t.ad_id = l.ad_id"
            " WHERE e.verdict = 'skip' AND l.first_seen >= ?"
            " AND json_extract(e.data, '$.stage') IN ('prefilter', 'market')"
            " AND COALESCE(json_extract(e.data, '$.found_by'), '') != 'ai_scout'"
            " AND (t.ad_id IS NULL OR t.source = 'script' OR (t.source = 'ai' AND t.interest >= ?))"
            " ORDER BY l.first_seen DESC LIMIT ?", (_ts(since), int(min_interest), max(0, int(limit))))
        return [Listing.model_validate_json(r["data"]) for r in rows]

    def scout_counts(self, since: datetime) -> dict[str, int]:
        """Scout readings and scout deals since `since` (the Состояние screen)."""
        read = self._query("SELECT COUNT(*) FROM scout_triage WHERE triaged_at >= ? AND source = 'ai'",
                           (_ts(since),))[0][0]
        found = self._query("SELECT COUNT(*) FROM evaluations WHERE evaluated_at >= ?"
                            " AND json_extract(data, '$.found_by') = 'ai_scout' AND verdict IN ('buy', 'maybe')",
                            (_ts(since),))[0][0]
        return {"read": int(read), "found": int(found)}

    def feedback_examples(self, *, limit: int = 5) -> dict[str, list[dict[str, Any]]]:
        """What the user told us: hidden deals (with the reason) and bought / sold ones (with prices),
        newest first — the scout's prompt learns the user's taste from them."""
        hidden = [dict(r) for r in self._query(
            "SELECT l.title AS title, l.price AS price, s.hidden_reason AS reason FROM deal_state s"
            " JOIN listings l ON l.ad_id = s.ad_id WHERE s.status = 'ignored'"
            " ORDER BY s.updated_at DESC LIMIT ?", (max(0, int(limit)),))]
        good = [dict(r) for r in self._query(
            "SELECT l.title AS title, l.price AS price, s.bought_price AS bought, s.sold_price AS sold,"
            " s.status AS status FROM deal_state s JOIN listings l ON l.ad_id = s.ad_id"
            " WHERE s.status IN ('bought', 'sold') ORDER BY s.updated_at DESC LIMIT ?", (max(0, int(limit)),))]
        return {"hidden": hidden, "good": good}

    def top_deals_since(self, since: datetime, *, verdicts: Iterable[str] = ("buy", "maybe"),
                        per_search: int = 3) -> list[DealView]:
        """The best evaluated deals since `since`, at most `per_search` per search (by score, then
        profit), hidden ones left out — the «Топ за день» digest."""
        wanted = [v for v in verdicts if v in ("buy", "maybe", "skip")] or ["buy"]
        rows = self._query(
            "SELECT l.data AS l_data, e.data AS e_data, s.status AS status, s.note AS note,"
            " l.search_name AS search_name FROM evaluations e JOIN listings l ON l.ad_id = e.ad_id"
            " LEFT JOIN deal_state s ON s.ad_id = l.ad_id"
            f" WHERE e.evaluated_at >= ? AND e.verdict IN ({','.join('?' * len(wanted))})"
            " AND COALESCE(s.status, 'new') != 'ignored'"
            " ORDER BY e.score DESC, COALESCE(e.profit, -1e9) DESC", (_ts(since), *wanted))
        taken: dict[str, int] = {}
        out: list[DealView] = []
        for r in rows:
            name = r["search_name"] or ""
            if taken.get(name, 0) >= max(1, int(per_search)):
                continue
            taken[name] = taken.get(name, 0) + 1
            out.append(DealView(listing=Listing.model_validate_json(r["l_data"]),
                                evaluation=Evaluation.model_validate_json(r["e_data"]),
                                status=r["status"] or "new", note=r["note"] or ""))
        return out

    # ---------------------------------------------------------- vision queue
    def queue_vision(self, ad_id: str, search_name: str = "", *, at: datetime | None = None) -> None:
        """Hold a would-be deal until the vision model is back (first queue time is kept)."""
        self._execute(
            "INSERT INTO vision_queue (ad_id, search_name, queued_at) SELECT ?, ?, ?"
            " WHERE EXISTS (SELECT 1 FROM listings WHERE ad_id = ?) ON CONFLICT(ad_id) DO NOTHING",
            (ad_id, search_name, _ts(at or utcnow()), ad_id))

    def vision_queue(self) -> list[tuple[str, str, datetime]]:
        """(ad_id, search_name, queued_at), oldest first."""
        return [(r["ad_id"], r["search_name"], datetime.fromisoformat(r["queued_at"]))
                for r in self._query("SELECT * FROM vision_queue ORDER BY queued_at")]

    def vision_queued_at(self, ad_id: str) -> datetime | None:
        rows = self._query("SELECT queued_at FROM vision_queue WHERE ad_id = ?", (ad_id,))
        return datetime.fromisoformat(rows[0]["queued_at"]) if rows else None

    def unqueue_vision(self, ad_ids: Iterable[str]) -> None:
        ids = list(dict.fromkeys(ad_ids))
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            self._execute(f"DELETE FROM vision_queue WHERE ad_id IN ({','.join('?' * len(chunk))})", chunk)

    # ------------------------------------------------------------- search state
    def search_has_run(self, name: str) -> bool:
        """Has this search completed a pass before? (Searches from before v0.2 count as run
        when they already found listings.)"""
        if self._query("SELECT 1 FROM search_state WHERE name = ?", (name,)):
            return True
        return bool(self._query("SELECT 1 FROM listings WHERE search_name = ? LIMIT 1", (name,)))

    def mark_search_run(self, name: str, *, baseline: bool = False) -> None:
        """Record a pass of `name`; `baseline` = this pass only learned prices."""
        now = _ts(utcnow())
        self._execute(
            "INSERT INTO search_state (name, first_run_at, last_run_at, baseline_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET last_run_at = excluded.last_run_at,"
            " baseline_at = COALESCE(excluded.baseline_at, search_state.baseline_at)",
            (name, now, now, now if baseline else None),
        )

    def search_baseline_at(self, name: str) -> datetime | None:
        """End of the learning-only first pass of `name` (listings seen before it are not new)."""
        rows = self._query("SELECT baseline_at FROM search_state WHERE name = ?", (name,))
        if not rows or not rows[0]["baseline_at"]:
            return None
        return datetime.fromisoformat(rows[0]["baseline_at"])

    # ---------------------------------------------------------------- kv state
    def get_state(self, key: str) -> tuple[str, datetime] | None:
        """(value, updated_at) of a small persistent flag, or None."""
        rows = self._query("SELECT value, updated_at FROM kv_state WHERE key = ?", (key,))
        return (rows[0]["value"], datetime.fromisoformat(rows[0]["updated_at"])) if rows else None

    def set_state(self, key: str, value: str = "", *, at: datetime | None = None) -> None:
        self._execute(
            "INSERT INTO kv_state (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, _ts(at or utcnow())),
        )

    # ----------------------------------------------------- delivery bookkeeping
    def note_delivery_failure(self, ad_id: str, channel: str, error: str) -> int:
        """Count a failed delivery of `ad_id` on `channel`; returns the attempts so far."""
        now = _ts(utcnow())
        self._execute(
            "INSERT INTO notify_attempts (ad_id, channel, attempts, first_at, last_at, last_error)"
            " VALUES (?, ?, 1, ?, ?, ?) ON CONFLICT(ad_id, channel) DO UPDATE SET"
            " attempts = notify_attempts.attempts + 1, last_at = excluded.last_at, last_error = excluded.last_error",
            (ad_id, channel, now, now, error[:500]),
        )
        rows = self._query("SELECT attempts FROM notify_attempts WHERE ad_id = ? AND channel = ?", (ad_id, channel))
        return int(rows[0]["attempts"]) if rows else 0

    def delivery_failures(self, ad_id: str, channel: str) -> tuple[int, datetime] | None:
        rows = self._query("SELECT attempts, first_at FROM notify_attempts WHERE ad_id = ? AND channel = ?",
                           (ad_id, channel))
        return (int(rows[0]["attempts"]), datetime.fromisoformat(rows[0]["first_at"])) if rows else None

    def retryable_deliveries(self, *, max_attempts: int, since: datetime) -> list[tuple[str, str]]:
        """(ad_id, channel) whose delivery failed fewer than `max_attempts` times, first after
        `since`, and that no later attempt delivered."""
        rows = self._query(
            "SELECT a.ad_id, a.channel FROM notify_attempts a"
            " WHERE a.attempts < ? AND a.first_at >= ?"
            " AND NOT EXISTS (SELECT 1 FROM notifications n WHERE n.ad_id = a.ad_id AND n.channel = a.channel)"
            " ORDER BY a.first_at",
            (max_attempts, _ts(since)),
        )
        return [(r["ad_id"], r["channel"]) for r in rows]

    def alerts_sent_since(self, since: datetime) -> int:
        """Deals delivered (on any real channel) since `since`."""
        return int(self._query(
            "SELECT COUNT(DISTINCT ad_id) FROM notifications WHERE sent_at >= ? AND channel NOT LIKE '\\_%' ESCAPE '\\'",
            (_ts(since),),
        )[0][0])

    def notified_since(self, since: datetime) -> list[Listing]:
        """Listings delivered to the user since `since` (for repost detection)."""
        rows = self._query(
            "SELECT l.data FROM listings l WHERE EXISTS (SELECT 1 FROM notifications n WHERE n.ad_id = l.ad_id"
            " AND n.sent_at >= ? AND n.channel NOT LIKE '\\_%' ESCAPE '\\')", (_ts(since),))
        return [Listing.model_validate_json(r["data"]) for r in rows]

    def queue_alert(self, ad_id: str) -> None:
        self._execute("INSERT OR IGNORE INTO alert_queue (ad_id, queued_at) VALUES (?, ?)", (ad_id, _ts(utcnow())))

    def queued_alerts(self) -> list[str]:
        return [r["ad_id"] for r in self._query("SELECT ad_id FROM alert_queue ORDER BY queued_at")]

    def unqueue_alerts(self, ad_ids: Iterable[str]) -> None:
        ids = list(ad_ids)
        if ids:
            self._execute(f"DELETE FROM alert_queue WHERE ad_id IN ({','.join('?' * len(ids))})", ids)

    # ------------------------------------------------------------------ backlog
    def pending_count(self, search_names: Iterable[str] | None = None, *, since: datetime | None = None) -> int:
        """Stored listings still waiting for an evaluation (the deferred backlog)."""
        sql = ("SELECT COUNT(*) FROM listings l LEFT JOIN evaluations e ON e.ad_id = l.ad_id"
               " WHERE e.ad_id IS NULL")
        params: list[Any] = []
        names = list(search_names) if search_names is not None else None
        if names is not None:
            if not names:
                return 0
            sql += f" AND l.search_name IN ({','.join('?' * len(names))})"
            params += names
        if since is not None:
            sql += " AND l.first_seen >= ?"
            params.append(_ts(since))
        return int(self._query(sql, params)[0][0])

    def stale_pending(self, search_name: str, *, before: datetime, after: datetime | None = None,
                      limit: int = 500) -> list[Listing]:
        """Pending listings of a search first seen before `before` (and after `after`, e.g. the
        end of the learning pass): the backlog gave up on them."""
        sql = ("SELECT l.data FROM listings l LEFT JOIN evaluations e ON e.ad_id = l.ad_id"
               " WHERE e.ad_id IS NULL AND l.search_name = ? AND l.first_seen < ?")
        params: list[Any] = [search_name, _ts(before)]
        if after is not None:
            sql += " AND l.first_seen > ?"
            params.append(_ts(after))
        sql += " ORDER BY l.first_seen LIMIT ?"
        params.append(limit)
        return [Listing.model_validate_json(r["data"]) for r in self._query(sql, params)]

    def expired_since(self, since: datetime) -> int:
        """Listings given up from the backlog in passes started since `since`."""
        rows = self._query("SELECT COALESCE(SUM(json_extract(data, '$.expired')), 0) FROM runs WHERE started_at >= ?",
                           (_ts(since),))
        return int(rows[0][0] or 0)

    # ------------------------------------------------------- migration / upkeep
    def migrate_v01_evaluations(self) -> int | None:
        """Once per database: drop evaluations written by v0.1 (no `stage`; their "buy"s used the
        old rules). Listings stay and are evaluated again. Returns the count, None if done before."""
        marker = "migration:v01_evaluations"
        if self.get_state(marker) is not None:
            return None
        removed = self._execute("DELETE FROM evaluations WHERE json_extract(data, '$.stage') IS NULL").rowcount
        self.set_state(marker, str(removed))
        return removed

    def apply_retention(self, *, listing_days: int = 60, run_days: int = 90) -> dict[str, int]:
        """Forget old noise: skipped listings (+ their evaluations, statuses, delivery records)
        not seen for `listing_days` — unless the user starred/contacted/bought them — and runs
        older than `run_days`. Price points are kept (they have their own window)."""
        cutoff = _ts(utcnow() - timedelta(days=listing_days))
        ids = [r["ad_id"] for r in self._query(
            "SELECT l.ad_id FROM listings l JOIN evaluations e ON e.ad_id = l.ad_id"
            " LEFT JOIN deal_state s ON s.ad_id = l.ad_id"
            " WHERE e.verdict = 'skip' AND l.last_seen < ?"
            " AND COALESCE(s.status, 'new') NOT IN ('starred', 'contacted', 'bought', 'sold')", (cutoff,))]
        with self._lock:
            try:
                for i in range(0, len(ids), 500):
                    chunk = ids[i:i + 500]
                    marks = ",".join("?" * len(chunk))
                    for table in ("deal_state", "notifications", "notify_attempts", "alert_queue", "listings"):
                        self._conn.execute(f"DELETE FROM {table} WHERE ad_id IN ({marks})", chunk)
                runs = self._conn.execute("DELETE FROM runs WHERE started_at < ?",
                                          (_ts(utcnow() - timedelta(days=run_days)),)).rowcount
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return {"listings": len(ids), "runs": runs}

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
            "potential_profit": one(  # open resale deals of the last week
                "SELECT COALESCE(SUM(e.profit), 0) FROM evaluations e"
                " JOIN listings l ON l.ad_id = e.ad_id"
                " LEFT JOIN deal_state s ON s.ad_id = e.ad_id"
                " WHERE e.verdict = 'buy' AND e.purpose = 'resale'"
                " AND COALESCE(s.status, 'new') NOT IN ('ignored', 'bought', 'sold') AND e.profit > 0"
                " AND l.first_seen >= ?",
                (_ts(utcnow() - timedelta(days=7)),),
            ),
            "bought_total": one("SELECT COUNT(*) FROM deal_state WHERE status IN ('bought', 'sold')"),
            "notified_total": one("SELECT COUNT(DISTINCT ad_id) FROM notifications"),
            "price_points_total": one("SELECT COUNT(*) FROM price_points"),
        }


    # ============================================================ web API v1 (additive)
    # Pipeline Избранное → Написал → Купил → Продал (starred → contacted → bought → sold): what the
    # user really paid / got / spent on top (fees, shipping), when, and why a deal was hidden.
    _DEAL_STATE_EXTRA = (
        ("bought_price", "REAL"), ("bought_at", "TEXT"), ("sold_price", "REAL"), ("sold_at", "TEXT"),
        ("extra_costs", "REAL"), ("hidden_reason", "TEXT"), ("contacted_at", "TEXT"), ("seen_at", "TEXT"),
    )
    _STATE_FIELDS = ("status", "note", "bought_price", "bought_at", "sold_price", "sold_at", "extra_costs",
                     "hidden_reason", "contacted_at", "seen_at")
    _STATE_TIMES = ("bought_at", "sold_at", "contacted_at", "seen_at", "updated_at")
    _UNSET: Any = object()
    _FRESH_SCORE = ("(COALESCE(e.score, -1) - MIN(20.0, MAX(0.0,"
                    " (julianday('now') - julianday(l.first_seen)) * 12.0)))")  # -0.5 points per hour, max -20
    _API_SORTS = {
        "best": f"{_FRESH_SCORE} DESC, l.first_seen DESC",
        "score": "COALESCE(e.score, -1) DESC, l.first_seen DESC",
        "fresh": "l.first_seen DESC",
        "newest": "l.first_seen DESC",
        "profit": "COALESCE(e.profit, -1e9) DESC, l.first_seen DESC",
        "price": "COALESCE(l.price, 1e9) ASC, l.first_seen DESC",
        "roi": "COALESCE(json_extract(e.data, '$.roi'), -1e9) DESC, l.first_seen DESC",
        "distance": "CASE WHEN json_extract(l.data, '$.distance_km') IS NULL THEN 1 ELSE 0 END,"
                    " json_extract(l.data, '$.distance_km') ASC, COALESCE(e.score, -1) DESC",
        "ending": "CASE WHEN json_extract(l.data, '$.ends_at') IS NULL"
                  " OR julianday(json_extract(l.data, '$.ends_at')) < julianday('now') THEN 1 ELSE 0 END,"
                  " julianday(json_extract(l.data, '$.ends_at')) ASC, l.first_seen DESC",
        "updated": "COALESCE(s.updated_at, l.first_seen) DESC, l.first_seen DESC",
    }
    # what to do with a deal, never "": the SQL twin of web.api.presenters.derive_action (an
    # auction is always "bid", buy + offer on a VB / best-offer ad -> "haggle", buy -> "buy",
    # maybe -> "watch", skip -> "skip"; a stored action wins, except "buy"/"haggle" on an auction)
    _AUCTION_SQL = "COALESCE(json_extract(l.data, '$.buying_options'), '[]') LIKE '%\"AUCTION\"%'"
    ACTION_SQL = (
        "(CASE"
        " WHEN e.ad_id IS NULL THEN 'watch'"
        " WHEN COALESCE(json_extract(e.data, '$.action'), '') != '' THEN"
        f"  CASE WHEN json_extract(e.data, '$.action') IN ('buy', 'haggle') AND {_AUCTION_SQL} THEN 'bid'"
        "  ELSE json_extract(e.data, '$.action') END"
        " WHEN e.verdict = 'skip' THEN 'skip'"
        f" WHEN {_AUCTION_SQL} THEN"
        "  CASE WHEN COALESCE(json_extract(e.data, '$.max_buy_price'), 0) > 0 THEN 'bid' ELSE 'watch' END"
        " WHEN e.verdict = 'buy' AND json_extract(e.data, '$.offer_price') IS NOT NULL"
        "  AND (COALESCE(json_extract(l.data, '$.negotiable'), 0) = 1"
        "   OR COALESCE(json_extract(l.data, '$.buying_options'), '[]') LIKE '%\"BEST_OFFER\"%') THEN 'haggle'"
        " WHEN e.verdict = 'buy' THEN 'buy'"
        " ELSE 'watch' END)"
    )
    _API_DEAL_SELECT = (
        "SELECT l.data AS l_data, e.data AS e_data, s.status AS status, s.note AS note,"
        " s.bought_price AS bought_price, s.bought_at AS bought_at, s.sold_price AS sold_price,"
        " s.sold_at AS sold_at, s.extra_costs AS extra_costs, s.hidden_reason AS hidden_reason,"
        " s.contacted_at AS contacted_at, s.seen_at AS seen_at, s.updated_at AS updated_at,"
        " EXISTS(SELECT 1 FROM notifications n WHERE n.ad_id = l.ad_id) AS notified"
        " FROM listings l"
        " LEFT JOIN evaluations e ON e.ad_id = l.ad_id"
        " LEFT JOIN deal_state s ON s.ad_id = l.ad_id"
    )
    _API_DEAL_COUNT = ("SELECT COUNT(*) FROM listings l LEFT JOIN evaluations e ON e.ad_id = l.ad_id"
                       " LEFT JOIN deal_state s ON s.ad_id = l.ad_id")

    def _migrate_deal_state(self) -> None:
        """deal_state columns of the pipeline (safe on old databases: only adds what's missing)."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(deal_state)")}
        for col, kind in self._DEAL_STATE_EXTRA:
            if col not in cols:
                self._conn.execute(f"ALTER TABLE deal_state ADD COLUMN {col} {kind}")

    @classmethod
    def _state_extra(cls, row: sqlite3.Row) -> dict[str, Any]:
        keys = set(row.keys())
        out: dict[str, Any] = {}
        for name in ("bought_price", "sold_price", "extra_costs", "hidden_reason"):
            out[name] = row[name] if name in keys else None
        for name in cls._STATE_TIMES:
            raw = row[name] if name in keys else None
            out[name] = datetime.fromisoformat(raw) if raw else None
        return out

    def deal_extras(self, ad_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Pipeline data per ad: bought/sold prices and times, extra costs, hidden reason, when the
        seller was contacted, when the user opened it (seen_at), last change (updated_at)."""
        ids = list(dict.fromkeys(ad_ids))
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            rows = self._query(f"SELECT * FROM deal_state WHERE ad_id IN ({','.join('?' * len(chunk))})", chunk)
            for row in rows:
                out[row["ad_id"]] = self._state_extra(row)
        return out

    def update_deal_state(self, ad_id: str, **changes: Any) -> dict[str, Any]:
        """Partial update of the user's state of a deal (keys of _STATE_FIELDS; datetimes allowed);
        fields left out keep their value. Returns deal_extras() after the change."""
        unknown = set(changes) - set(self._STATE_FIELDS)
        if unknown:
            raise ValueError(f"unknown deal_state fields: {sorted(unknown)}")
        with self._lock:
            row = self._conn.execute("SELECT * FROM deal_state WHERE ad_id = ?", (ad_id,)).fetchone()
            current: dict[str, Any] = {name: None for name in self._STATE_FIELDS}
            current.update(status="new", note="")
            if row is not None:
                current.update({name: row[name] for name in self._STATE_FIELDS if name in row.keys()})
            for key, value in changes.items():
                current[key] = _ts(value) if isinstance(value, datetime) else value
            if current["status"] not in DEAL_STATUSES:
                raise ValueError(f"unknown status {current['status']!r}; expected one of {DEAL_STATUSES}")
            current["note"] = current["note"] or ""
            cols = ["ad_id", *self._STATE_FIELDS, "updated_at"]
            values = [ad_id, *(current[name] for name in self._STATE_FIELDS), _ts(utcnow())]
            updates = ", ".join(f"{c} = excluded.{c}" for c in cols[1:])
            self._conn.execute(
                f"INSERT INTO deal_state ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
                f" ON CONFLICT(ad_id) DO UPDATE SET {updates}", values)
            self._conn.commit()
        return self.deal_extras([ad_id]).get(ad_id, {})

    def mark_seen(self, ad_ids: Iterable[str], *, at: datetime | None = None) -> int:
        """The user opened these deals (the feed's "не смотрел" filter). Returns how many were new."""
        when = _ts(at or utcnow())
        count = 0
        with self._lock:
            for ad_id in dict.fromkeys(ad_ids):
                cur = self._conn.execute(
                    "INSERT INTO deal_state (ad_id, status, note, updated_at, seen_at)"
                    " SELECT ?, 'new', '', ?, ? WHERE EXISTS (SELECT 1 FROM listings WHERE ad_id = ?)"
                    " ON CONFLICT(ad_id) DO UPDATE SET seen_at = COALESCE(deal_state.seen_at, excluded.seen_at)"
                    " WHERE deal_state.seen_at IS NULL", (ad_id, when, when, ad_id))
                count += cur.rowcount
            self._conn.commit()
        return count

    @staticmethod
    def _api_deal_where(
        *,
        verdicts: list[str] | None = None,
        actions: list[str] | None = None,
        statuses: list[str] | None = None,
        include_ignored: bool = False,
        purpose: str | None = None,
        source: str | None = None,
        search_names: list[str] | None = None,
        q: str | None = None,
        min_score: float | None = None,
        min_profit: float | None = None,
        min_price: float | None = None,
        max_price: float | None = None,
        max_km: float | None = None,
        shipping: bool | None = None,
        no_flags: bool = False,
        unseen: bool = False,
        ai_checked: bool | None = None,
        actionable: bool = False,
        since: datetime | None = None,
        ad_ids: list[str] | None = None,
    ) -> tuple[str, list[Any]]:
        where: list[str] = []
        params: list[Any] = []

        def among(expr: str, values: list[Any]) -> None:
            where.append(f"{expr} IN ({','.join('?' * len(values))})")
            params.extend(values)

        if verdicts:
            parts: list[str] = []
            real = [v for v in verdicts if v != "none"]
            if real:
                parts.append(f"e.verdict IN ({','.join('?' * len(real))})")
                params.extend(real)
            if "none" in verdicts:
                parts.append("e.ad_id IS NULL")
            where.append("(" + " OR ".join(parts) + ")")
        if actions:
            among(Database.ACTION_SQL, actions)
        if actionable:  # worth acting on: a "buy" verdict or a buy / haggle / bid action
            where.append(f"(e.verdict = 'buy' OR {Database.ACTION_SQL} IN ('buy', 'haggle', 'bid'))")
        if statuses:
            among("COALESCE(s.status, 'new')", statuses)
        elif not include_ignored:
            where.append("COALESCE(s.status, 'new') != 'ignored'")
        if purpose:
            where.append("e.purpose = ?")
            params.append(purpose)
        if source:
            where.append("COALESCE(json_extract(l.data, '$.source'), 'kleinanzeigen') = ?")
            params.append(source)
        if search_names:
            among("l.search_name", search_names)
        if ad_ids:
            among("l.ad_id", ad_ids)
        if q:
            where.append("(LOWER(l.title) LIKE ? OR l.ad_id = ?)")
            params += [f"%{q.lower()}%", q.strip()]
        if min_score is not None:
            where.append("e.score >= ?")
            params.append(min_score)
        if min_profit is not None:
            where.append("e.profit >= ?")
            params.append(min_profit)
        if min_price is not None:
            where.append("l.price >= ?")
            params.append(min_price)
        if max_price is not None:
            where.append("l.price <= ?")
            params.append(max_price)
        if max_km is not None:
            where.append("json_extract(l.data, '$.distance_km') <= ?")
            params.append(max_km)
        if shipping is True:
            where.append("(json_extract(l.data, '$.shipping_possible') = 1"
                         " OR json_extract(l.data, '$.shipping_cost') IS NOT NULL)")
        elif shipping is False:
            where.append("(COALESCE(json_extract(l.data, '$.shipping_possible'), 0) = 0"
                         " AND json_extract(l.data, '$.shipping_cost') IS NULL)")
        if no_flags:
            where.append("COALESCE(json_array_length(json_extract(e.data, '$.red_flags')), 0) = 0"
                         " AND COALESCE(json_array_length(json_extract(e.data, '$.ai.red_flags')), 0) = 0"
                         " AND COALESCE(json_array_length(json_extract(e.data, '$.ai_second.red_flags')), 0) = 0")
        if unseen:
            where.append("s.seen_at IS NULL")
        if ai_checked is not None:
            where.append("json_extract(e.data, '$.ai_checked') = ?")
            params.append(1 if ai_checked else 0)
        if since is not None:
            where.append("l.first_seen >= ?")
            params.append(_ts(since))
        return (" WHERE " + " AND ".join(where)) if where else "", params

    def find_deals(
        self, *, sort: str = "best", limit: int = 50, offset: int = 0, **filters: Any
    ) -> tuple[list[tuple[DealView, dict[str, Any]]], int]:
        """Deals for the JSON API (filters of _api_deal_where, sorts of _API_SORTS, a page).
        -> ([(deal, deal_extras), ...], total matching)."""
        where, params = self._api_deal_where(**filters)
        order = self._API_SORTS.get(sort, self._API_SORTS["best"])
        rows = self._query(self._API_DEAL_SELECT + where + f" ORDER BY {order} LIMIT ? OFFSET ?",
                           params + [max(0, int(limit)), max(0, int(offset))]) if limit > 0 else []
        total = int(self._query(self._API_DEAL_COUNT + where, params)[0][0])
        return [(self._deal_from_row(r), self._state_extra(r)) for r in rows], total

    def count_deals_v1(self, **filters: Any) -> int:
        where, params = self._api_deal_where(**filters)
        return int(self._query(self._API_DEAL_COUNT + where, params)[0][0])

    def get_deal_extras(self, ad_id: str) -> tuple[DealView, dict[str, Any]] | None:
        rows = self._query(self._API_DEAL_SELECT + " WHERE l.ad_id = ?", (ad_id,))
        return (self._deal_from_row(rows[0]), self._state_extra(rows[0])) if rows else None

    def pipeline_rows(self) -> list[dict[str, Any]]:
        """Every deal on the pipeline (starred / contacted / bought / sold) with its money data."""
        rows = self._query(
            "SELECT s.ad_id, s.status, s.bought_price, s.bought_at, s.sold_price, s.sold_at, s.extra_costs,"
            " s.contacted_at, s.updated_at, e.profit AS expected_profit, e.purpose AS purpose,"
            " json_extract(e.data, '$.estimate.market_price') AS market_price, l.price AS price"
            " FROM deal_state s JOIN listings l ON l.ad_id = s.ad_id LEFT JOIN evaluations e ON e.ad_id = s.ad_id"
            " WHERE s.status IN ('starred', 'contacted', 'bought', 'sold')")
        out = []
        for r in rows:
            item = {k: r[k] for k in r.keys()}
            for name in ("bought_at", "sold_at", "contacted_at", "updated_at"):
                item[name] = datetime.fromisoformat(item[name]) if item[name] else None
            out.append(item)
        return out

    def search_stats(self) -> dict[str, dict[str, Any]]:
        """Per search name: ads seen (total / 24 h), buy / maybe verdicts (total / 7 days), still
        unevaluated, newest ad, and search_state (first/last run, end of the learning pass)."""
        day_ago = _ts(utcnow() - timedelta(days=1))
        week_ago = _ts(utcnow() - timedelta(days=7))
        out: dict[str, dict[str, Any]] = {}
        for r in self._query(
            "SELECT l.search_name AS name, COUNT(*) AS ads,"
            " SUM(CASE WHEN l.first_seen >= ? THEN 1 ELSE 0 END) AS ads_24h,"
            " SUM(CASE WHEN e.verdict = 'buy' THEN 1 ELSE 0 END) AS buy,"
            " SUM(CASE WHEN e.verdict = 'buy' AND l.first_seen >= ? THEN 1 ELSE 0 END) AS buy_7d,"
            " SUM(CASE WHEN e.verdict = 'maybe' THEN 1 ELSE 0 END) AS maybe,"
            " SUM(CASE WHEN e.ad_id IS NULL THEN 1 ELSE 0 END) AS pending,"
            " MAX(l.first_seen) AS last_new_at"
            " FROM listings l LEFT JOIN evaluations e ON e.ad_id = l.ad_id"
            " WHERE l.search_name != '' GROUP BY l.search_name", (day_ago, week_ago)):
            out[r["name"]] = {
                "ads": int(r["ads"] or 0), "ads_24h": int(r["ads_24h"] or 0), "buy": int(r["buy"] or 0),
                "buy_7d": int(r["buy_7d"] or 0), "maybe": int(r["maybe"] or 0), "pending": int(r["pending"] or 0),
                "last_new_at": datetime.fromisoformat(r["last_new_at"]) if r["last_new_at"] else None,
            }
        empty = {"ads": 0, "ads_24h": 0, "buy": 0, "buy_7d": 0, "maybe": 0, "pending": 0, "last_new_at": None}
        for r in self._query("SELECT name, first_run_at, last_run_at, baseline_at FROM search_state"):
            info = out.setdefault(r["name"], dict(empty))
            for key in ("first_run_at", "last_run_at", "baseline_at"):
                info[key] = datetime.fromisoformat(r[key]) if r[key] else None
        for info in out.values():
            for key in ("first_run_at", "last_run_at", "baseline_at"):
                info.setdefault(key, None)
        return out

    def activity_since(self, since: datetime) -> dict[str, list[Any]]:
        """Raw timestamps (ISO text) for statistics per day, all since `since`: listings
        (first_seen), evaluated (evaluated_at), deals ((first_seen, verdict, purpose, profit, status,
        action) of buy/maybe), notified (first delivery per ad), bought ((when, price)), sold ((when,
        bought_price, sold_price, extra_costs))."""
        s = _ts(since)
        return {
            "listings": [r[0] for r in self._query("SELECT first_seen FROM listings WHERE first_seen >= ?", (s,))],
            "evaluated": [r[0] for r in self._query(
                "SELECT evaluated_at FROM evaluations WHERE evaluated_at >= ?", (s,))],
            "deals": [tuple(r) for r in self._query(
                "SELECT l.first_seen, e.verdict, e.purpose, e.profit, COALESCE(st.status, 'new'),"
                f" {self.ACTION_SQL}"
                " FROM evaluations e JOIN listings l ON l.ad_id = e.ad_id"
                " LEFT JOIN deal_state st ON st.ad_id = e.ad_id"
                " WHERE e.verdict IN ('buy', 'maybe') AND l.first_seen >= ?", (s,))],
            "notified": [r[0] for r in self._query(
                "SELECT MIN(sent_at) AS first FROM notifications WHERE channel NOT LIKE '\\_%' ESCAPE '\\'"
                " GROUP BY ad_id HAVING MIN(sent_at) >= ?", (s,))],
            "bought": [tuple(r) for r in self._query(
                "SELECT COALESCE(bought_at, updated_at), bought_price FROM deal_state"
                " WHERE status IN ('bought', 'sold') AND COALESCE(bought_at, updated_at) >= ?", (s,))],
            "sold": [tuple(r) for r in self._query(
                "SELECT COALESCE(sold_at, updated_at), bought_price, sold_price, extra_costs FROM deal_state"
                " WHERE status = 'sold' AND COALESCE(sold_at, updated_at) >= ?", (s,))],
        }

    def notify_candidates(self, since: datetime, *, min_score: float, verdicts: list[str]) -> list[str]:
        """first_seen of evaluated deals since `since` that pass a notification threshold
        (verdict + score, not hidden, not no_alert) — for "how many alerts would I have got"."""
        if not verdicts:
            return []
        rows = self._query(
            "SELECT l.first_seen FROM evaluations e JOIN listings l ON l.ad_id = e.ad_id"
            " LEFT JOIN deal_state s ON s.ad_id = e.ad_id"
            f" WHERE e.verdict IN ({','.join('?' * len(verdicts))}) AND e.score >= ? AND l.first_seen >= ?"
            " AND COALESCE(s.status, 'new') != 'ignored'"
            " AND COALESCE(json_extract(e.data, '$.no_alert'), 0) = 0",
            [*verdicts, min_score, _ts(since)])
        return [r[0] for r in rows]

    def delivery_problems(self, since: datetime) -> list[dict[str, Any]]:
        """Channels with deliveries that failed since `since` and never went through."""
        rows = self._query(
            "SELECT a.channel AS channel, COUNT(*) AS failed, MAX(a.last_at) AS last_at, a.last_error AS last_error"
            " FROM notify_attempts a WHERE a.last_at >= ?"
            " AND NOT EXISTS (SELECT 1 FROM notifications n WHERE n.ad_id = a.ad_id AND n.channel = a.channel)"
            " GROUP BY a.channel ORDER BY a.channel", (_ts(since),))
        return [{"channel": r["channel"], "failed": int(r["failed"]),
                 "last_at": datetime.fromisoformat(r["last_at"]) if r["last_at"] else None,
                 "last_error": r["last_error"] or ""} for r in rows]

    def last_deliveries(self) -> dict[str, datetime]:
        """Per real channel: when the last deal was delivered."""
        rows = self._query("SELECT channel, MAX(sent_at) AS last FROM notifications"
                           " WHERE channel NOT LIKE '\\_%' ESCAPE '\\' GROUP BY channel")
        return {r["channel"]: datetime.fromisoformat(r["last"]) for r in rows if r["last"]}

    def delete_listings(self, ad_ids: Iterable[str]) -> int:
        """Forget listings with everything attached (evaluation, state, deliveries, own price point)."""
        ids = list(dict.fromkeys(ad_ids))
        removed = 0
        with self._lock:
            try:
                for i in range(0, len(ids), 500):
                    chunk = ids[i:i + 500]
                    marks = ",".join("?" * len(chunk))
                    for table in ("evaluations", "deal_state", "notifications", "notify_attempts", "alert_queue",
                                  "price_points", "scout_triage", "vision_queue"):
                        self._conn.execute(f"DELETE FROM {table} WHERE ad_id IN ({marks})", chunk)
                    removed += self._conn.execute(f"DELETE FROM listings WHERE ad_id IN ({marks})", chunk).rowcount
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return removed

    def delete_runs(self, run_ids: Iterable[int]) -> int:
        ids = [int(i) for i in run_ids]
        if not ids:
            return 0
        return self._execute(f"DELETE FROM runs WHERE id IN ({','.join('?' * len(ids))})", ids).rowcount

    def reset_price_history(self) -> int:
        """Forget every remembered price (the market is learned again from scratch)."""
        with self._lock:
            self._conn.execute("DELETE FROM price_point_words")
            removed = self._conn.execute("DELETE FROM price_points").rowcount
            self._conn.commit()
        return removed

    def reset_all(self) -> dict[str, int]:
        """Delete all data (listings, verdicts, statuses, runs, prices, bookkeeping); searches in the
        config stay and start with a new learning pass."""
        counts: dict[str, int] = {}
        with self._lock:
            try:
                for table in ("price_point_words", "price_points", "scout_triage", "vision_queue", "evaluations",
                              "deal_state", "notifications", "notify_attempts", "alert_queue", "listings", "runs",
                              "search_state"):
                    counts[table] = self._conn.execute(f"DELETE FROM {table}").rowcount
                self._conn.execute("DELETE FROM kv_state WHERE key NOT LIKE 'migration:%'"
                                   " AND key NOT LIKE 'onboarding%' AND key NOT LIKE 'monitor:%'")
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            try:  # give the space back: "delete everything" must not make the file bigger
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._conn.execute("VACUUM")
            except sqlite3.Error as exc:  # e.g. another connection is reading: harmless
                log.info("VACUUM after reset skipped: %s", exc)
        return counts

    def backup_to(self, target: str | Path) -> None:
        """Consistent copy of the database file (SQLite online backup)."""
        dest = sqlite3.connect(str(target))
        try:
            with self._lock:
                self._conn.backup(dest)
        finally:
            dest.close()

    def table_counts(self) -> dict[str, int]:
        return {t: int(self._query(f"SELECT COUNT(*) FROM {t}")[0][0])
                for t in ("listings", "evaluations", "price_points", "runs", "deal_state")}

    # ================================================= «Сборки» (build projects, additive)
    def _migrate_projects(self) -> None:
        """Tables of ebeyparser.projects (CREATE IF NOT EXISTS: safe on any old database; the
        queries live in ebeyparser/projects/store.py)."""
        self._conn.executescript(PROJECTS_SCHEMA)


# Build projects («Сборки»): a plan of slots with alternatives, the searches that track them, and
# the alerts already sent. JSON `data` columns hold the details (see ebeyparser/projects/models.py).
PROJECTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL DEFAULT '',
    goal         TEXT NOT NULL DEFAULT '',
    template     TEXT NOT NULL DEFAULT 'custom',
    budget       REAL,
    status       TEXT NOT NULL DEFAULT 'draft',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    data         TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS project_slots (
    project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    slot         TEXT NOT NULL,
    position     INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'open',
    chosen       TEXT NOT NULL DEFAULT '',
    data         TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (project_id, slot)
);

CREATE TABLE IF NOT EXISTS project_options (
    project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    slot         TEXT NOT NULL,
    option       TEXT NOT NULL,
    position     INTEGER NOT NULL DEFAULT 0,
    kb_key       TEXT,
    qty          INTEGER NOT NULL DEFAULT 1,
    target_price REAL,
    max_price    REAL,
    data         TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (project_id, slot, option)
);

-- searches (config names) created by «Начать отслеживание» for a slot's option
CREATE TABLE IF NOT EXISTS project_searches (
    project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    slot         TEXT NOT NULL,
    option       TEXT NOT NULL,
    search_name  TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (project_id, search_name)
);
CREATE INDEX IF NOT EXISTS idx_project_searches_name ON project_searches(search_name);

-- alerts already sent per project (one per ad and kind)
CREATE TABLE IF NOT EXISTS project_alerts (
    project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    ad_id        TEXT NOT NULL,
    kind         TEXT NOT NULL,
    slot         TEXT NOT NULL DEFAULT '',
    price        REAL,
    total        REAL,
    text         TEXT NOT NULL DEFAULT '',
    delivered    INTEGER NOT NULL DEFAULT 0,
    sent_at      TEXT NOT NULL,
    PRIMARY KEY (project_id, ad_id, kind)
);
"""
