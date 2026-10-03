from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

ACTIVE_ORDER_STATUSES = ("scheduled", "rescheduled")


def slot_name(resource_type: str, resource_name: str) -> str:
    return f"{resource_type}:{resource_name}"


def venue_slot(venue: str) -> str:
    return f"venue:{venue}"


class RescheduleRepository:
    """封装仪式订单与批量改期预演的 SQLite 读写。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    # ---- 仪式订单 ----

    def order_by_id(self, order_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM ceremony_orders WHERE id=?", (order_id,)).fetchone()

    def order_by_code(self, order_code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM ceremony_orders WHERE order_code=?", (order_code,)).fetchone()

    def create_order(self, *, order_code: str, ceremony_type: str, ceremony_date: str, venue: str, customer_name: str, base_fee_cents: int, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO ceremony_orders(order_code,ceremony_type,ceremony_date,venue,customer_name,base_fee_cents,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (order_code, ceremony_type, ceremony_date, venue, customer_name, base_fee_cents, now, now),
        )
        return dict(self.order_by_id(cursor.lastrowid))

    def add_resource(self, *, order_id: int, resource_type: str, resource_name: str, commitment_fee_cents: int, now: str) -> None:
        self.connection.execute(
            "INSERT INTO ceremony_order_resources(order_id,resource_type,resource_name,commitment_fee_cents,created_at) VALUES(?,?,?,?,?)",
            (order_id, resource_type, resource_name, commitment_fee_cents, now),
        )

    def resources_of(self, order_ids: Iterable[int]) -> dict[int, list[dict[str, Any]]]:
        ids = list(order_ids)
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = self.connection.execute(
            f"SELECT * FROM ceremony_order_resources WHERE order_id IN ({placeholders}) ORDER BY id",
            ids,
        ).fetchall()
        result: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            result.setdefault(int(row["order_id"]), []).append(dict(row))
        return result

    def list_orders(self, *, date: str | None, status: str | None, limit: int = 200) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if date:
            clauses.append("ceremony_date=?")
            values.append(date)
        if status:
            clauses.append("status=?")
            values.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(limit)
        rows = self.connection.execute("SELECT * FROM ceremony_orders" + where + " ORDER BY ceremony_date,id LIMIT ?", values).fetchall()
        return [dict(row) for row in rows]

    def orders_on_date(self, date: str, statuses: tuple[str, ...] = ACTIVE_ORDER_STATUSES) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in statuses)
        rows = self.connection.execute(
            f"SELECT * FROM ceremony_orders WHERE ceremony_date=? AND status IN ({placeholders}) ORDER BY id",
            (date, *statuses),
        ).fetchall()
        return [dict(row) for row in rows]

    def occupied_slots(self, date: str, exclude_order_ids: Iterable[int]) -> set[str]:
        """返回指定日期已被在效订单占用的资源槽位（含场地槽位）。"""
        excluded = list(exclude_order_ids)
        params: list[Any] = [date]
        exclusion = ""
        if excluded:
            placeholders = ",".join("?" for _ in excluded)
            exclusion = f" AND o.id NOT IN ({placeholders})"
            params.extend(excluded)
        rows = self.connection.execute(
            "SELECT o.venue AS venue,r.resource_type AS resource_type,r.resource_name AS resource_name"
            " FROM ceremony_orders o LEFT JOIN ceremony_order_resources r ON r.order_id=o.id"
            " WHERE o.ceremony_date=? AND o.status IN ('scheduled','rescheduled')" + exclusion,
            params,
        ).fetchall()
        slots = {venue_slot(str(row["venue"])) for row in rows}
        slots.update(slot_name(str(row["resource_type"]), str(row["resource_name"])) for row in rows if row["resource_name"])
        return slots

    def apply_order_move(self, *, order_id: int, target_date: str, fee_delta_cents: int, now: str) -> None:
        self.connection.execute(
            "UPDATE ceremony_orders SET ceremony_date=?,status='rescheduled',reschedule_fee_cents=reschedule_fee_cents+?,updated_at=?,version=version+1 WHERE id=?",
            (target_date, fee_delta_cents, now, order_id),
        )

    # ---- 预演与报告 ----

    def create_rehearsal(self, *, rehearsal_code: str, source_date: str, window_start: str, window_end: str, request_reason: str, requested_by: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO reschedule_rehearsals(rehearsal_code,source_date,window_start,window_end,request_reason,requested_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (rehearsal_code, source_date, window_start, window_end, request_reason, requested_by, now, now),
        )
        return dict(self.rehearsal_by_id(cursor.lastrowid))

    def rehearsal_by_id(self, rehearsal_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM reschedule_rehearsals WHERE id=?", (rehearsal_id,)).fetchone()

    def list_rehearsals(self, *, status: str | None, limit: int = 100) -> list[dict[str, Any]]:
        if status:
            rows = self.connection.execute("SELECT * FROM reschedule_rehearsals WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)).fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM reschedule_rehearsals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def rehearsals_in_status(self, status: str) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM reschedule_rehearsals WHERE status=? ORDER BY id", (status,)).fetchall()
        return [dict(row) for row in rows]

    def expired_awaiting(self, now: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM reschedule_rehearsals WHERE status='awaiting_confirmation' AND expires_at IS NOT NULL AND expires_at<=? ORDER BY id",
            (now,),
        ).fetchall()
        return [dict(row) for row in rows]

    def finish_analysis(self, rehearsal_id: int, *, status: str, target_date: str | None, report_version: int, report_digest: str, expires_at: str | None, failure_reason: str, now: str) -> None:
        self.connection.execute(
            "UPDATE reschedule_rehearsals SET status=?,target_date=?,report_version=?,report_digest=?,expires_at=?,failure_reason=?,updated_at=?,version=version+1 WHERE id=?",
            (status, target_date, report_version, report_digest, expires_at, failure_reason, now, rehearsal_id),
        )

    def mark_terminal(self, rehearsal_id: int, *, status: str, failure_reason: str, now: str) -> None:
        self.connection.execute(
            "UPDATE reschedule_rehearsals SET status=?,failure_reason=?,updated_at=?,version=version+1 WHERE id=?",
            (status, failure_reason, now, rehearsal_id),
        )

    def mark_applied(self, rehearsal_id: int, *, now: str) -> None:
        self.connection.execute(
            "UPDATE reschedule_rehearsals SET status='applied',applied_at=?,updated_at=?,version=version+1 WHERE id=?",
            (now, now, rehearsal_id),
        )

    def insert_report(self, *, rehearsal_id: int, version: int, target_date: str | None, summary: dict[str, Any], details: dict[str, Any], report_digest: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO reschedule_reports(rehearsal_id,version,target_date,summary_json,details_json,report_digest,generated_at) VALUES(?,?,?,?,?,?,?)",
            (rehearsal_id, version, target_date, json.dumps(summary, ensure_ascii=False, sort_keys=True), json.dumps(details, ensure_ascii=False, sort_keys=True), report_digest, now),
        )
        return dict(self.connection.execute("SELECT * FROM reschedule_reports WHERE id=?", (cursor.lastrowid,)).fetchone())

    def latest_report(self, rehearsal_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM reschedule_reports WHERE rehearsal_id=? ORDER BY version DESC LIMIT 1", (rehearsal_id,)).fetchone()

    # ---- 确认 ----

    def confirmations_for(self, rehearsal_id: int, report_version: int | None = None) -> list[dict[str, Any]]:
        if report_version is None:
            rows = self.connection.execute("SELECT * FROM reschedule_confirmations WHERE rehearsal_id=? ORDER BY id", (rehearsal_id,)).fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM reschedule_confirmations WHERE rehearsal_id=? AND report_version=? ORDER BY id", (rehearsal_id, report_version)).fetchall()
        return [dict(row) for row in rows]

    def insert_confirmation(self, *, rehearsal_id: int, report_version: int, report_digest: str, permission_group: str, actor: str, actor_name: str, note: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO reschedule_confirmations(rehearsal_id,report_version,report_digest,permission_group,actor,actor_name,note,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (rehearsal_id, report_version, report_digest, permission_group, actor, actor_name, note, now),
        )
        return dict(self.connection.execute("SELECT * FROM reschedule_confirmations WHERE id=?", (cursor.lastrowid,)).fetchone())

    # ---- 事件与迁移记录 ----

    def insert_event(self, *, rehearsal_id: int, action: str, actor: str, reason: str, details: dict[str, Any] | None, now: str) -> None:
        self.connection.execute(
            "INSERT INTO reschedule_events(rehearsal_id,action,actor,reason,details_json,created_at) VALUES(?,?,?,?,?,?)",
            (rehearsal_id, action, actor, reason, json.dumps(details or {}, ensure_ascii=False, sort_keys=True), now),
        )

    def events_for(self, rehearsal_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM reschedule_events WHERE rehearsal_id=? ORDER BY id", (rehearsal_id,)).fetchall()
        return [dict(row) for row in rows]

    def insert_move(self, *, rehearsal_id: int, order_id: int, order_code: str, from_date: str, to_date: str, fee_delta_cents: int, moved_resources: list[dict[str, Any]], now: str) -> None:
        self.connection.execute(
            "INSERT INTO reschedule_order_moves(rehearsal_id,order_id,order_code,from_date,to_date,fee_delta_cents,moved_resources_json,applied_at) VALUES(?,?,?,?,?,?,?,?)",
            (rehearsal_id, order_id, order_code, from_date, to_date, fee_delta_cents, json.dumps(moved_resources, ensure_ascii=False, sort_keys=True), now),
        )

    def moves_for(self, rehearsal_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM reschedule_order_moves WHERE rehearsal_id=? ORDER BY id", (rehearsal_id,)).fetchall()
        return [dict(row) for row in rows]
