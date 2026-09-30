"""SQLite storage of build projects (tables created by Database._migrate_projects in db.py)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Iterable

from ..db import Database
from ..models import utcnow
from .models import AlertRecord, Plan, PlanOption, PlanSlot, Project, Purchase, Requirements, SearchLink

_PROJECT_DATA = ("requirements", "location", "radius_km", "ai", "notes", "tracking_since", "fits_alerted")
_SLOT_DATA = ("label", "kind", "when", "per_gpu", "hint", "note", "purchases")
_OPTION_DATA = ("label", "query", "why", "source", "target_by")
_ALERT_DATA = ("title_ru", "detail_ru", "url")


def _ts(dt: datetime) -> str:
    return dt.isoformat()


def _dt(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw) if raw else None


class ProjectStore:
    """CRUD of projects, their slots/options, linked searches and sent alerts."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ----------------------------------------------------------------- helpers
    def _rows(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.db._query(sql, tuple(params))

    def _write(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.db._execute(sql, tuple(params))

    # ---------------------------------------------------------------- projects
    def create(self, plan: Plan, *, status: str = "draft") -> Project:
        now = utcnow()
        fields = plan.model_dump(include=set(Plan.model_fields))
        project = Project.model_validate({**fields, "status": status, "created_at": now, "updated_at": now})
        with self.db._lock:
            cur = self.db._conn.execute(
                "INSERT INTO projects (name, goal, template, budget, status, created_at, updated_at, data)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (project.name, project.goal, project.template, project.budget, project.status, _ts(now), _ts(now),
                 self._project_data(project)))
            project.id = int(cur.lastrowid or 0)
            self._write_slots(project)
            self.db._conn.commit()
        return project

    def save(self, project: Project) -> Project:
        project.updated_at = utcnow()
        with self.db._lock:
            try:
                self.db._conn.execute(
                    "UPDATE projects SET name = ?, goal = ?, template = ?, budget = ?, status = ?, updated_at = ?,"
                    " data = ? WHERE id = ?",
                    (project.name, project.goal, project.template, project.budget, project.status,
                     _ts(project.updated_at), self._project_data(project), project.id))
                self._write_slots(project)
                self.db._conn.commit()
            except Exception:
                self.db._conn.rollback()
                raise
        return project

    def touch(self, project_id: int) -> None:
        self._write("UPDATE projects SET updated_at = ? WHERE id = ?", (_ts(utcnow()), project_id))

    def get(self, project_id: int) -> Project | None:
        rows = self._rows("SELECT * FROM projects WHERE id = ?", (int(project_id),))
        return self._load(rows[0]) if rows else None

    def exists(self, project_id: int) -> bool:
        return bool(self._rows("SELECT 1 FROM projects WHERE id = ?", (int(project_id),)))

    def list_projects(self, *, status: str | None = None) -> list[Project]:
        sql, params = "SELECT * FROM projects", []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        rows = self._rows(sql + " ORDER BY updated_at DESC, id DESC", params)
        return [self._load(r) for r in rows]

    def delete(self, project_id: int) -> bool:
        with self.db._lock:
            conn = self.db._conn
            try:
                for table in ("project_alerts", "project_searches", "project_options", "project_slots"):
                    conn.execute(f"DELETE FROM {table} WHERE project_id = ?", (project_id,))
                removed = conn.execute("DELETE FROM projects WHERE id = ?", (project_id,)).rowcount
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return removed > 0

    @staticmethod
    def _project_data(project: Project) -> str:
        data = project.model_dump(mode="json", include=set(_PROJECT_DATA))
        return json.dumps(data, ensure_ascii=False)

    def _write_slots(self, project: Project) -> None:
        conn = self.db._conn
        conn.execute("DELETE FROM project_options WHERE project_id = ?", (project.id,))
        conn.execute("DELETE FROM project_slots WHERE project_id = ?", (project.id,))
        for pos, slot in enumerate(project.slots):
            data = slot.model_dump(mode="json", include=set(_SLOT_DATA))
            conn.execute(
                "INSERT INTO project_slots (project_id, slot, position, status, chosen, data) VALUES (?, ?, ?, ?, ?, ?)",
                (project.id, slot.key, pos, slot.status, slot.chosen or "", json.dumps(data, ensure_ascii=False)))
            for opos, opt in enumerate(slot.options):
                odata = opt.model_dump(mode="json", include=set(_OPTION_DATA))
                conn.execute(
                    "INSERT INTO project_options (project_id, slot, option, position, kb_key, qty, target_price,"
                    " max_price, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (project.id, slot.key, opt.key, opos, opt.kb_key, opt.qty, opt.target_price, opt.max_price,
                     json.dumps(odata, ensure_ascii=False)))

    def _load(self, row: sqlite3.Row) -> Project:
        data = json.loads(row["data"] or "{}")
        pid = int(row["id"])
        options: dict[str, list[PlanOption]] = {}
        for o in self._rows("SELECT * FROM project_options WHERE project_id = ? ORDER BY slot, position", (pid,)):
            odata = json.loads(o["data"] or "{}")
            options.setdefault(o["slot"], []).append(PlanOption(
                key=o["option"], kb_key=o["kb_key"], qty=int(o["qty"] or 1), target_price=o["target_price"],
                max_price=o["max_price"], **{k: odata[k] for k in _OPTION_DATA if k in odata}))
        slots: list[PlanSlot] = []
        for s in self._rows("SELECT * FROM project_slots WHERE project_id = ? ORDER BY position", (pid,)):
            sdata = json.loads(s["data"] or "{}")
            purchases = [Purchase.model_validate(p) for p in sdata.pop("purchases", []) or []]
            slots.append(PlanSlot(key=s["slot"], status=s["status"], chosen=s["chosen"] or None,
                                  options=options.get(s["slot"], []), purchases=purchases,
                                  **{k: sdata[k] for k in _SLOT_DATA if k in sdata and k != "purchases"}))
        return Project(
            id=pid, name=row["name"], goal=row["goal"], template=row["template"], budget=row["budget"],
            status=row["status"], created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]), slots=slots,
            requirements=Requirements.model_validate(data.get("requirements") or {}),
            location=data.get("location") or "", radius_km=data.get("radius_km"), ai=data.get("ai"),
            notes=list(data.get("notes") or []), tracking_since=_dt(data.get("tracking_since")),
            fits_alerted=bool(data.get("fits_alerted")),
        )

    # ---------------------------------------------------------------- searches
    def link_search(self, project_id: int, slot: str, option: str, search_name: str) -> None:
        self._write("INSERT INTO project_searches (project_id, slot, option, search_name, created_at)"
                    " VALUES (?, ?, ?, ?, ?) ON CONFLICT(project_id, search_name) DO UPDATE SET"
                    " slot = excluded.slot, option = excluded.option",
                    (project_id, slot, option, search_name, _ts(utcnow())))

    def unlink_search(self, project_id: int, search_name: str) -> None:
        self._write("DELETE FROM project_searches WHERE project_id = ? AND search_name = ?", (project_id, search_name))

    def searches(self, project_id: int) -> list[SearchLink]:
        rows = self._rows("SELECT * FROM project_searches WHERE project_id = ? ORDER BY created_at, search_name",
                          (project_id,))
        return [SearchLink(project_id=r["project_id"], slot=r["slot"], option=r["option"],
                           search_name=r["search_name"], created_at=datetime.fromisoformat(r["created_at"]))
                for r in rows]

    def projects_for_search(self, search_name: str) -> list[int]:
        rows = self._rows("SELECT DISTINCT project_id FROM project_searches WHERE search_name = ?", (search_name,))
        return [int(r[0]) for r in rows]

    def search_counts(self) -> dict[int, int]:
        rows = self._rows("SELECT project_id, COUNT(*) AS n FROM project_searches GROUP BY project_id")
        return {int(r["project_id"]): int(r["n"]) for r in rows}

    # ------------------------------------------------------------------ offers
    def offer_rows(self, search_names: Iterable[str], since: datetime, *, limit: int = 600) -> list[sqlite3.Row]:
        """Priced listings of these searches seen since `since`, with evaluation and user status."""
        names = list(dict.fromkeys(n for n in search_names if n))
        if not names:
            return []
        marks = ",".join("?" * len(names))
        return self._rows(
            "SELECT l.ad_id AS ad_id, l.search_name AS search_name, l.data AS l_data, l.first_seen AS first_seen,"
            " l.last_seen AS last_seen, e.data AS e_data, s.status AS status"
            " FROM listings l LEFT JOIN evaluations e ON e.ad_id = l.ad_id LEFT JOIN deal_state s ON s.ad_id = l.ad_id"
            f" WHERE l.search_name IN ({marks}) AND l.price IS NOT NULL AND l.price > 0 AND l.last_seen >= ?"
            " ORDER BY l.price ASC LIMIT ?", [*names, _ts(since), max(1, int(limit))])

    def listing_search(self, ad_id: str) -> str | None:
        rows = self._rows("SELECT search_name FROM listings WHERE ad_id = ?", (ad_id,))
        return rows[0]["search_name"] if rows else None

    # ------------------------------------------------------------------ alerts
    @staticmethod
    def _alert_data(alert: AlertRecord) -> str:
        return json.dumps({k: getattr(alert, k) for k in _ALERT_DATA}, ensure_ascii=False)

    def claim_alert(self, alert: AlertRecord) -> bool:
        """Record an alert before sending it; False when it was already recorded (sent before)."""
        cur = self._write(
            "INSERT OR IGNORE INTO project_alerts (project_id, ad_id, kind, slot, price, total, text, delivered, sent_at,"
            " data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (alert.project_id, alert.ad_id, alert.kind, alert.slot, alert.price, alert.total, alert.text,
             int(alert.delivered), _ts(alert.sent_at), self._alert_data(alert)))
        return cur.rowcount > 0

    def set_alert(self, alert: AlertRecord) -> None:
        self._write("UPDATE project_alerts SET text = ?, delivered = ?, total = ?, data = ? WHERE project_id = ?"
                    " AND ad_id = ? AND kind = ?", (alert.text, int(alert.delivered), alert.total,
                                                    self._alert_data(alert), alert.project_id, alert.ad_id, alert.kind))

    def drop_alert(self, project_id: int, ad_id: str, kind: str) -> None:
        self._write("DELETE FROM project_alerts WHERE project_id = ? AND ad_id = ? AND kind = ?",
                    (project_id, ad_id, kind))

    def alerted(self, project_id: int) -> set[tuple[str, str]]:
        return {(r["ad_id"], r["kind"]) for r in self._rows(
            "SELECT ad_id, kind FROM project_alerts WHERE project_id = ?", (project_id,))}

    def alerts(self, project_id: int, limit: int = 20) -> list[AlertRecord]:
        rows = self._rows("SELECT * FROM project_alerts WHERE project_id = ? ORDER BY sent_at DESC LIMIT ?",
                          (project_id, max(1, int(limit))))
        out = []
        for r in rows:
            try:
                data = json.loads(r["data"] or "{}")
            except (IndexError, KeyError, ValueError):
                data = {}
            out.append(AlertRecord(project_id=r["project_id"], ad_id=r["ad_id"], kind=r["kind"], slot=r["slot"],
                                   price=r["price"], total=r["total"], text=r["text"], delivered=bool(r["delivered"]),
                                   sent_at=datetime.fromisoformat(r["sent_at"]),
                                   **{k: str(data[k]) for k in _ALERT_DATA if isinstance(data, dict) and data.get(k)}))
        return out

    def alert_counts(self) -> dict[int, tuple[int, datetime | None]]:
        rows = self._rows("SELECT project_id, COUNT(*) AS n, MAX(sent_at) AS last FROM project_alerts GROUP BY project_id")
        return {int(r["project_id"]): (int(r["n"]), _dt(r["last"])) for r in rows}


__all__ = ["ProjectStore"]
