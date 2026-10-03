from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.database import get_connection, transaction

SCHEMA = """
CREATE TABLE IF NOT EXISTS ceremony_resources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_type TEXT NOT NULL CHECK(resource_type IN ('staff','vehicle','supplier')),
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    daily_capacity INTEGER NOT NULL CHECK(daily_capacity > 0),
    change_fee_cents INTEGER NOT NULL DEFAULT 0 CHECK(change_fee_cents >= 0),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ceremony_bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES compute_tasks(id) ON DELETE CASCADE,
    resource_id INTEGER NOT NULL REFERENCES ceremony_resources(id) ON DELETE RESTRICT,
    booking_date TEXT NOT NULL,
    units INTEGER NOT NULL CHECK(units > 0),
    commitment TEXT NOT NULL DEFAULT 'confirmed' CHECK(commitment IN ('tentative','confirmed')),
    fee_cents INTEGER NOT NULL DEFAULT 0 CHECK(fee_cents >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(task_id, resource_id)
);
CREATE INDEX IF NOT EXISTS idx_ceremony_bookings_date ON ceremony_bookings(resource_id, booking_date);
CREATE TABLE IF NOT EXISTS reschedule_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requested_by TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    source_date TEXT NOT NULL,
    candidate_start TEXT NOT NULL,
    candidate_end TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'rehearsing'
        CHECK(status IN ('rehearsing','report_ready','approved','applying','applied','expired','failed','cancelled')),
    report_version INTEGER NOT NULL DEFAULT 0,
    report_digest TEXT NOT NULL DEFAULT '',
    report_json TEXT NOT NULL DEFAULT '{}',
    report_expires_at TEXT NOT NULL DEFAULT '',
    report_ttl_minutes INTEGER NOT NULL DEFAULT 30 CHECK(report_ttl_minutes > 0),
    failure_reason TEXT NOT NULL DEFAULT '',
    apply_batch_key TEXT NOT NULL DEFAULT '',
    applied_at TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(requested_by, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_reschedule_plans_status ON reschedule_plans(status, updated_at);
CREATE TABLE IF NOT EXISTS reschedule_confirmations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES reschedule_plans(id) ON DELETE CASCADE,
    report_version INTEGER NOT NULL,
    report_digest TEXT NOT NULL,
    domain TEXT NOT NULL CHECK(domain IN ('operations','finance')),
    actor TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(plan_id, report_version, domain)
);
CREATE TABLE IF NOT EXISTS reschedule_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES reschedule_plans(id) ON DELETE CASCADE,
    report_version INTEGER NOT NULL,
    report_digest TEXT NOT NULL,
    applied_by TEXT NOT NULL,
    confirmer_operations TEXT NOT NULL,
    confirmer_finance TEXT NOT NULL,
    source_date TEXT NOT NULL,
    target_date TEXT NOT NULL,
    moved_tasks INTEGER NOT NULL,
    moved_bookings INTEGER NOT NULL,
    commitment_resets INTEGER NOT NULL,
    fee_delta_cents INTEGER NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(plan_id)
);
CREATE TABLE IF NOT EXISTS reschedule_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES reschedule_plans(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reschedule_events_plan ON reschedule_events(plan_id, id);
"""

CONFIRM_DOMAINS = ("operations", "finance")
MOVABLE_TASK_STATUS = "queued"
AFFECTED_TASK_STATUSES = ("queued", "running", "cancel_requested")
MAX_CANDIDATE_DAYS = 14


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def _date_range(start: date, end: date) -> list[date]:
    days: list[date] = []
    current = start
    while current <= end:
        days.append(current)
        current += timedelta(days=1)
    return days


def ensure_schema() -> None:
    get_connection().executescript(SCHEMA)


def reconcile_interrupted(clock: Clock | None = None) -> dict[str, list[int]]:
    """服务启动对账：崩溃遗留的 rehearsing/applying 计划安全失败，绝不视为已执行。"""
    now = to_storage((clock or SystemClock()).now())
    recovered: dict[str, list[int]] = {"rehearsing": [], "applying": []}
    reasons = {
        "rehearsing": "服务重启时预演未完成，已安全放弃",
        "applying": "服务重启时切换未提交，订单数据已回滚，计划安全失败",
    }
    with transaction(immediate=True) as connection:
        rows = connection.execute("SELECT id,status FROM reschedule_plans WHERE status IN ('rehearsing','applying') ORDER BY id").fetchall()
        for row in rows:
            status = str(row["status"])
            connection.execute(
                "UPDATE reschedule_plans SET status='failed',failure_reason=?,updated_at=?,version=version+1 WHERE id=? AND status=?",
                (reasons[status], now, row["id"], status),
            )
            connection.execute(
                "INSERT INTO reschedule_events(plan_id,action,actor,detail_json,created_at) VALUES(?,?,?,?,?)",
                (row["id"], "restart_interrupted", "system", _dump({"previous_status": status, "reason": reasons[status]}), now),
            )
            recovered[status].append(int(row["id"]))
    return recovered


class ApplySafetyError(Exception):
    """切换阶段的安全失败：不修改任何生效数据，原因写入计划。"""


class RescheduleService:
    """批量改期：先预演生成只读报告，双人分权确认后原子切换。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------
    # 资源与预订维护
    # ------------------------------------------------------------------
    def create_resource(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            if connection.execute("SELECT 1 FROM ceremony_resources WHERE code=?", (payload["code"],)).fetchone():
                raise ConflictError("资源编码已存在")
            cursor = connection.execute(
                "INSERT INTO ceremony_resources(resource_type,code,name,daily_capacity,change_fee_cents,active,created_at) VALUES(?,?,?,?,?,1,?)",
                (payload["resource_type"], payload["code"], payload["name"], payload["daily_capacity"], payload["change_fee_cents"], now),
            )
            return dict(connection.execute("SELECT * FROM ceremony_resources WHERE id=?", (cursor.lastrowid,)).fetchone())

    def list_resources(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM ceremony_resources ORDER BY resource_type, code").fetchall()
        return [dict(row) for row in rows]

    def create_booking(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            task = connection.execute("SELECT * FROM compute_tasks WHERE id=?", (payload["task_id"],)).fetchone()
            if task is None:
                raise NotFoundError("仪式订单不存在")
            resource = connection.execute("SELECT * FROM ceremony_resources WHERE code=?", (payload["resource_code"],)).fetchone()
            if resource is None:
                raise NotFoundError("资源不存在")
            booking_date = payload.get("booking_date") or str(task["available_at"])[:10]
            try:
                cursor = connection.execute(
                    "INSERT INTO ceremony_bookings(task_id,resource_id,booking_date,units,commitment,fee_cents,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (task["id"], resource["id"], booking_date, payload["units"], payload["commitment"], payload["fee_cents"], now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("该订单已预订此资源") from exc
            return dict(connection.execute("SELECT * FROM ceremony_bookings WHERE id=?", (cursor.lastrowid,)).fetchone())

    def list_bookings(self, *, booking_date: str | None = None, task_id: int | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if booking_date:
            clauses.append("b.booking_date=?")
            params.append(booking_date)
        if task_id is not None:
            clauses.append("b.task_id=?")
            params.append(task_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            "SELECT b.*,r.code AS resource_code,r.resource_type FROM ceremony_bookings b JOIN ceremony_resources r ON r.id=b.resource_id" + where + " ORDER BY b.booking_date,b.id",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # 预演
    # ------------------------------------------------------------------
    def create_plan(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        self._validate_window(payload)
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM reschedule_plans WHERE requested_by=? AND idempotency_key=?",
                (actor, payload["idempotency_key"]),
            ).fetchone()
            if existing is not None:
                same = (
                    existing["source_date"] == payload["source_date"]
                    and existing["candidate_start"] == payload["candidate_start"]
                    and existing["candidate_end"] == payload["candidate_end"]
                    and existing["reason"] == payload["reason"]
                )
                if not same:
                    raise ConflictError("同一幂等键对应了不同的改期请求")
                return self._plan_view(connection, existing["id"])
            affected = self._affected_tasks(connection, payload["source_date"])
            if not affected:
                raise ValidationError("所选日期没有需要改期的仪式订单")
            cursor = connection.execute(
                "INSERT INTO reschedule_plans(requested_by,idempotency_key,source_date,candidate_start,candidate_end,reason,status,report_ttl_minutes,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,'rehearsing',?,?,?)",
                (
                    actor,
                    payload["idempotency_key"],
                    payload["source_date"],
                    payload["candidate_start"],
                    payload["candidate_end"],
                    payload["reason"],
                    payload["report_ttl_minutes"],
                    now,
                    now,
                ),
            )
            plan_id = int(cursor.lastrowid)
            self._add_event(connection, plan_id, "created", actor, {"source_date": payload["source_date"], "reason": payload["reason"]}, now)
        return self._run_rehearsal(plan_id, actor)

    def rehearse(self, plan_id: int, actor: str) -> dict[str, Any]:
        self._refresh_expiry(plan_id)
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            plan = self._require_plan(connection, plan_id)
            if plan["status"] not in {"report_ready", "approved", "expired"}:
                raise ConflictError("当前状态不允许重新预演", context={"status": plan["status"]})
            affected = self._affected_tasks(connection, plan["source_date"])
            if not affected:
                raise ValidationError("所选日期已没有需要改期的仪式订单")
            connection.execute(
                "UPDATE reschedule_plans SET status='rehearsing',failure_reason='',updated_at=?,version=version+1 WHERE id=?",
                (now, plan_id),
            )
            self._add_event(connection, plan_id, "rehearse_requested", actor, {"previous_report_version": plan["report_version"]}, now)
        return self._run_rehearsal(plan_id, actor)

    def _run_rehearsal(self, plan_id: int, actor: str) -> dict[str, Any]:
        """第二阶段：只读计算后一次性写入报告；崩溃时计划停留在 rehearsing，由启动对账安全失败。"""
        now_value = self.clock.now()
        now = to_storage(now_value)
        try:
            with transaction(immediate=True) as connection:
                plan = self._require_plan(connection, plan_id)
                if plan["status"] != "rehearsing":
                    raise ConflictError("计划不在预演状态", context={"status": plan["status"]})
                report = self._compute_report(connection, plan, now_value)
                version = int(plan["report_version"]) + 1
                expires_at = to_storage(now_value + timedelta(minutes=int(plan["report_ttl_minutes"])))
                connection.execute(
                    "UPDATE reschedule_plans SET status='report_ready',report_version=?,report_digest=?,report_json=?,report_expires_at=?,failure_reason='',updated_at=?,version=version+1 WHERE id=?",
                    (version, report["report_digest"], _dump(report), expires_at, now, plan_id),
                )
                self._add_event(
                    connection,
                    plan_id,
                    "report_ready",
                    actor,
                    {
                        "report_version": version,
                        "report_digest": report["report_digest"],
                        "feasible": report["feasible"],
                        "selected_date": report["selected_date"],
                        "expires_at": expires_at,
                    },
                    now,
                )
                return self._plan_view(connection, plan_id)
        except ConflictError:
            raise
        except Exception as exc:
            self._mark_failed(plan_id, actor, f"预演执行异常：{exc}", now, from_status="rehearsing")
            raise

    def _compute_report(self, connection: sqlite3.Connection, plan: sqlite3.Row, now: datetime) -> dict[str, Any]:
        source_date = plan["source_date"]
        candidate_days = _date_range(date.fromisoformat(plan["candidate_start"]), date.fromisoformat(plan["candidate_end"]))
        tasks = self._affected_tasks(connection, source_date)
        task_ids = [int(task["id"]) for task in tasks]
        bookings = self._bookings_of(connection, task_ids)
        resource_ids = sorted({int(booking["resource_id"]) for booking in bookings})
        resources = self._resources_by_id(connection, resource_ids)
        usage = self._usage_on_dates(connection, [day.isoformat() for day in candidate_days], resource_ids, exclude_task_ids=task_ids)

        order_conflicts: list[dict[str, Any]] = []
        for task in tasks:
            if task["status"] != MOVABLE_TASK_STATUS:
                order_conflicts.append(
                    {
                        "kind": "order_not_movable",
                        "task_id": int(task["id"]),
                        "status": task["status"],
                        "reason": f"订单状态为 {task['status']}，只有排队中的订单可以改期",
                    }
                )

        migrations: list[dict[str, Any]] = []
        for booking in bookings:
            resource = resources[int(booking["resource_id"])]
            migrations.append(
                {
                    "booking_id": int(booking["id"]),
                    "task_id": int(booking["task_id"]),
                    "resource_code": resource["code"],
                    "resource_type": resource["resource_type"],
                    "units": int(booking["units"]),
                    "commitment": booking["commitment"],
                    "from_date": booking["booking_date"],
                    "fee_cents": int(booking["fee_cents"]),
                    "change_fee_cents": int(resource["change_fee_cents"]) * int(booking["units"]),
                    "commitment_reset": booking["commitment"] == "confirmed" and resource["resource_type"] == "supplier",
                }
            )

        needed_by_resource: dict[int, int] = {}
        for booking in bookings:
            needed_by_resource[int(booking["resource_id"])] = needed_by_resource.get(int(booking["resource_id"]), 0) + int(booking["units"])

        date_evaluations: list[dict[str, Any]] = []
        for day in candidate_days:
            day_key = day.isoformat()
            blockers: list[dict[str, Any]] = list(order_conflicts)
            usage_view: list[dict[str, Any]] = []
            for resource_id in resource_ids:
                resource = resources[resource_id]
                needed = needed_by_resource.get(resource_id, 0)
                used_by_others = int(usage.get((resource_id, day_key), 0))
                capacity = int(resource["daily_capacity"])
                ok = bool(resource["active"]) and used_by_others + needed <= capacity
                usage_view.append(
                    {
                        "resource_code": resource["code"],
                        "needed": needed,
                        "used_by_others": used_by_others,
                        "capacity": capacity,
                        "ok": ok,
                    }
                )
                if not resource["active"]:
                    blockers.append({"kind": "resource_inactive", "date": day_key, "resource_code": resource["code"], "reason": "资源已停用"})
                elif used_by_others + needed > capacity:
                    blockers.append(
                        {
                            "kind": "resource_unavailable",
                            "date": day_key,
                            "resource_code": resource["code"],
                            "needed": needed,
                            "available": capacity - used_by_others,
                            "reason": "候选日期资源容量不足",
                        }
                    )
            date_evaluations.append({"date": day_key, "feasible": not blockers, "blocking": blockers, "resource_usage": usage_view})

        selected = next((item["date"] for item in date_evaluations if item["feasible"]), None)
        resource_conflicts = [blocker for item in date_evaluations for blocker in item["blocking"] if blocker["kind"] != "order_not_movable"]
        fee_differences = [
            {
                "booking_id": item["booking_id"],
                "task_id": item["task_id"],
                "resource_code": item["resource_code"],
                "original_fee_cents": item["fee_cents"],
                "change_fee_cents": item["change_fee_cents"],
                "reason": "改期手续费",
            }
            for item in migrations
        ]
        input_digest = _digest(
            {
                "source_date": source_date,
                "candidate_start": plan["candidate_start"],
                "candidate_end": plan["candidate_end"],
                "tasks": [[int(t["id"]), t["status"], t["available_at"], int(t["version"])] for t in tasks],
                "bookings": [
                    [int(b["id"]), int(b["task_id"]), int(b["resource_id"]), b["booking_date"], int(b["units"]), b["commitment"], int(b["fee_cents"])]
                    for b in bookings
                ],
                "resources": [[rid, int(resources[rid]["daily_capacity"]), int(resources[rid]["active"]), int(resources[rid]["change_fee_cents"])] for rid in resource_ids],
                "usage": [[rid, day, int(amount)] for (rid, day), amount in sorted(usage.items())],
            }
        )
        report: dict[str, Any] = {
            "source_date": source_date,
            "candidate_range": {"start": plan["candidate_start"], "end": plan["candidate_end"]},
            "generated_at": to_storage(now),
            "selected_date": selected,
            "feasible": selected is not None,
            "affected_orders": [
                {
                    "task_id": int(task["id"]),
                    "template_code": task["template_code"],
                    "project_code": task["project_code"],
                    "requested_by": task["requested_by"],
                    "status": task["status"],
                    "scheduled_at": task["available_at"],
                    "reschedulable": task["status"] == MOVABLE_TASK_STATUS,
                }
                for task in tasks
            ],
            "conflicts": order_conflicts + resource_conflicts,
            "date_evaluations": date_evaluations,
            "resource_migrations": migrations,
            "fee_differences": fee_differences,
            "totals": {
                "orders": len(tasks),
                "movable_orders": len(tasks) - len(order_conflicts),
                "conflicts": len(order_conflicts) + len(resource_conflicts),
                "bookings": len(bookings),
                "commitment_resets": sum(1 for item in migrations if item["commitment_reset"]),
                "fee_delta_cents": sum(item["change_fee_cents"] for item in fee_differences),
            },
            "input_digest": input_digest,
        }
        report["report_digest"] = _digest(report)
        return report

    # ------------------------------------------------------------------
    # 双人确认
    # ------------------------------------------------------------------
    def confirm(self, plan_id: int, *, domain: str, actor: str, report_version: int, note: str, permissions: frozenset[str]) -> dict[str, Any]:
        if domain not in CONFIRM_DOMAINS:
            raise ValidationError("确认权限域必须是 operations 或 finance")
        required = f"reschedule.confirm.{domain}"
        if required not in permissions and "*" not in permissions:
            raise PermissionDeniedError(f"缺少权限：{required}")
        self._refresh_expiry(plan_id)
        now = to_storage(self.clock.now())
        rejection: tuple[str, dict[str, Any]] | None = None
        with transaction(immediate=True) as connection:
            plan = self._require_plan(connection, plan_id)
            report = json.loads(plan["report_json"]) if plan["report_json"] else {}
            if plan["status"] not in {"report_ready", "approved"}:
                rejection = ("当前状态不允许确认", {"domain": domain, "reason": "当前状态不允许确认", "status": plan["status"]})
            elif int(plan["report_version"]) != report_version:
                rejection = ("确认的报告版本与当前报告不一致，请复核最新报告", {"domain": domain, "reason": "确认的报告版本与当前报告不一致", "expected": plan["report_version"], "provided": report_version})
            elif not report.get("feasible"):
                rejection = ("报告存在阻塞冲突，不可确认", {"domain": domain, "reason": "报告存在阻塞冲突，不可确认"})
            else:
                existing = connection.execute(
                    "SELECT * FROM reschedule_confirmations WHERE plan_id=? AND report_version=? AND domain=?",
                    (plan_id, report_version, domain),
                ).fetchone()
                same_actor = connection.execute(
                    "SELECT 1 FROM reschedule_confirmations WHERE plan_id=? AND report_version=? AND actor=?",
                    (plan_id, report_version, actor),
                ).fetchone()
                if existing is not None:
                    rejection = (f"权限域 {domain} 已确认过，不允许重复确认", {"domain": domain, "reason": f"权限域 {domain} 已由 {existing['actor']} 确认，重复确认被拒绝"})
                elif same_actor is not None:
                    rejection = ("同一人员不能对同一份报告重复确认，需要两名不同人员分权确认", {"domain": domain, "reason": "同一人员不能对同一份报告重复确认"})
            if rejection is None:
                connection.execute(
                    "INSERT INTO reschedule_confirmations(plan_id,report_version,report_digest,domain,actor,note,created_at) VALUES(?,?,?,?,?,?,?)",
                    (plan_id, report_version, plan["report_digest"], domain, actor, note, now),
                )
                self._add_event(connection, plan_id, "confirmed", actor, {"domain": domain, "report_version": report_version}, now)
                confirmed_domains = {
                    row["domain"]
                    for row in connection.execute("SELECT domain FROM reschedule_confirmations WHERE plan_id=? AND report_version=?", (plan_id, report_version)).fetchall()
                }
                if all(item in confirmed_domains for item in CONFIRM_DOMAINS):
                    connection.execute("UPDATE reschedule_plans SET status='approved',updated_at=?,version=version+1 WHERE id=?", (now, plan_id))
                    self._add_event(connection, plan_id, "approved", actor, {"report_version": report_version, "domains": sorted(confirmed_domains)}, now)
                else:
                    connection.execute("UPDATE reschedule_plans SET updated_at=?,version=version+1 WHERE id=?", (now, plan_id))
                view = self._plan_view(connection, plan_id)
                view["confirmations"] = self._confirmations(connection, plan_id, report_version)
                return view
        with transaction(immediate=True) as connection:
            self._add_event(connection, plan_id, "confirmation_rejected", actor, rejection[1], now)
        raise ConflictError(rejection[0])

    # ------------------------------------------------------------------
    # 原子切换
    # ------------------------------------------------------------------
    def apply(self, plan_id: int, actor: str) -> dict[str, Any]:
        self._refresh_expiry(plan_id)
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            plan = self._require_plan(connection, plan_id)
            if plan["status"] != "approved":
                raise ConflictError("计划未获得完整的双人确认，不能切换", context={"status": plan["status"]})
            connection.execute("UPDATE reschedule_plans SET status='applying',updated_at=?,version=version+1 WHERE id=?", (now, plan_id))
            self._add_event(connection, plan_id, "apply_started", actor, {"report_version": plan["report_version"]}, now)
        try:
            with transaction(immediate=True) as connection:
                result = self._apply_atomic(connection, plan_id, actor, now)
        except ApplySafetyError as exc:
            self._mark_failed(plan_id, actor, exc.args[0], now, from_status="applying")
            raise ConflictError(exc.args[0]) from exc
        except Exception as exc:
            self._mark_failed(plan_id, actor, f"切换执行异常：{exc}", now, from_status="applying")
            raise
        return result

    def _apply_atomic(self, connection: sqlite3.Connection, plan_id: int, actor: str, now: str) -> dict[str, Any]:
        plan = self._require_plan(connection, plan_id)
        if plan["status"] != "applying":
            raise ApplySafetyError("计划不在切换状态")
        report = json.loads(plan["report_json"])
        target_date = report.get("selected_date")
        if not target_date:
            raise ApplySafetyError("报告没有可选目标日期，不能切换")

        tasks = self._affected_tasks(connection, plan["source_date"])
        task_ids = [int(task["id"]) for task in tasks]
        bookings = self._bookings_of(connection, task_ids)
        resource_ids = sorted({int(booking["resource_id"]) for booking in bookings})
        resources = self._resources_by_id(connection, resource_ids)
        usage = self._usage_on_dates(connection, [target_date], resource_ids, exclude_task_ids=task_ids)
        needed_by_resource: dict[int, int] = {}
        for booking in bookings:
            needed_by_resource[int(booking["resource_id"])] = needed_by_resource.get(int(booking["resource_id"]), 0) + int(booking["units"])
        shortages: list[dict[str, Any]] = []
        for resource_id, needed in needed_by_resource.items():
            resource = resources[resource_id]
            available = int(resource["daily_capacity"]) - int(usage.get((resource_id, target_date), 0))
            if not resource["active"] or available < needed:
                shortages.append({"resource_code": resource["code"], "needed": needed, "available": max(available, 0)})
        if shortages:
            raise ApplySafetyError("部分资源在目标日期不可用，切换已安全取消：" + "、".join(item["resource_code"] for item in shortages))

        current_digest = self._current_input_digest(connection, plan)
        if current_digest != report["input_digest"]:
            raise ApplySafetyError("预演后订单、资源或预订数据已变化，报告失效，请重新预演")

        batch_key = f"reschedule-plan-{plan_id}-v{plan['report_version']}"
        moved_tasks: list[dict[str, Any]] = []
        for task in tasks:
            before = dict(task)
            scheduled = from_storage(task["available_at"]) or datetime.now(UTC)
            moved = datetime.combine(date.fromisoformat(target_date), scheduled.timetz())
            connection.execute(
                "UPDATE compute_tasks SET available_at=?,updated_at=?,version=version+1 WHERE id=?",
                (to_storage(moved), now, task["id"]),
            )
            after = dict(connection.execute("SELECT * FROM compute_tasks WHERE id=?", (task["id"],)).fetchone())
            connection.execute(
                "INSERT INTO compute_interventions(task_id,actor,action,reason,before_json,after_json,batch_key,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (task["id"], actor, "reschedule", plan["reason"], _dump(before), _dump(after), batch_key, now),
            )
            moved_tasks.append({"task_id": int(task["id"]), "from": before["available_at"], "to": after["available_at"]})

        moved_bookings: list[dict[str, Any]] = []
        commitment_resets = 0
        fee_delta = 0
        for booking in bookings:
            resource = resources[int(booking["resource_id"])]
            change_fee = int(resource["change_fee_cents"]) * int(booking["units"])
            reset = booking["commitment"] == "confirmed" and resource["resource_type"] == "supplier"
            commitment_resets += 1 if reset else 0
            fee_delta += change_fee
            connection.execute(
                "UPDATE ceremony_bookings SET booking_date=?,fee_cents=fee_cents+?,commitment=?,updated_at=? WHERE id=?",
                (target_date, change_fee, "tentative" if reset else booking["commitment"], now, booking["id"]),
            )
            moved_bookings.append(
                {
                    "booking_id": int(booking["id"]),
                    "task_id": int(booking["task_id"]),
                    "resource_code": resource["code"],
                    "to_date": target_date,
                    "change_fee_cents": change_fee,
                    "commitment_reset": reset,
                }
            )

        confirmations = self._confirmations(connection, plan_id, int(plan["report_version"]))
        confirmer = {row["domain"]: row["actor"] for row in confirmations}
        detail = {
            "report": report,
            "moved_tasks": moved_tasks,
            "moved_bookings": moved_bookings,
            "confirmations": confirmations,
        }
        connection.execute(
            "INSERT INTO reschedule_results(plan_id,report_version,report_digest,applied_by,confirmer_operations,confirmer_finance,source_date,target_date,moved_tasks,moved_bookings,commitment_resets,fee_delta_cents,detail_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                plan_id,
                plan["report_version"],
                plan["report_digest"],
                actor,
                confirmer.get("operations", ""),
                confirmer.get("finance", ""),
                plan["source_date"],
                target_date,
                len(moved_tasks),
                len(moved_bookings),
                commitment_resets,
                fee_delta,
                _dump(detail),
                now,
            ),
        )
        connection.execute(
            "UPDATE reschedule_plans SET status='applied',applied_at=?,apply_batch_key=?,updated_at=?,version=version+1 WHERE id=?",
            (now, batch_key, now, plan_id),
        )
        self._add_event(connection, plan_id, "applied", actor, {"target_date": target_date, "moved_tasks": len(moved_tasks), "moved_bookings": len(moved_bookings), "fee_delta_cents": fee_delta}, now)
        return self.final_version(plan_id, connection=connection)

    # ------------------------------------------------------------------
    # 取消与查询
    # ------------------------------------------------------------------
    def cancel(self, plan_id: int, actor: str, reason: str) -> dict[str, Any]:
        self._refresh_expiry(plan_id)
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            plan = self._require_plan(connection, plan_id)
            if plan["status"] not in {"report_ready", "approved", "expired"}:
                raise ConflictError("当前状态不允许取消", context={"status": plan["status"]})
            connection.execute("UPDATE reschedule_plans SET status='cancelled',failure_reason=?,updated_at=?,version=version+1 WHERE id=?", (reason, now, plan_id))
            self._add_event(connection, plan_id, "cancelled", actor, {"reason": reason}, now)
            return self._plan_view(connection, plan_id)

    def get_plan(self, plan_id: int) -> dict[str, Any]:
        self._refresh_expiry(plan_id)
        with transaction(immediate=True) as connection:
            view = self._plan_view(connection, plan_id)
            view["events"] = [dict(row) for row in connection.execute("SELECT * FROM reschedule_events WHERE plan_id=? ORDER BY id", (plan_id,)).fetchall()]
            return view

    def list_plans(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        clauses = ""
        params: list[Any] = []
        if status:
            clauses = " WHERE status=?"
            params.append(status)
        params.append(max(1, min(limit, 500)))
        rows = self.connection.execute("SELECT * FROM reschedule_plans" + clauses + " ORDER BY id DESC LIMIT ?", params).fetchall()
        return [self._plan_summary(dict(row)) for row in rows]

    def get_report(self, plan_id: int, *, full: bool = False) -> dict[str, Any]:
        plan = self._require_plan(self.connection, plan_id)
        if int(plan["report_version"]) == 0:
            raise NotFoundError("预演报告尚未生成")
        report = json.loads(plan["report_json"])
        summary = {
            "plan_id": plan_id,
            "report_version": plan["report_version"],
            "report_digest": plan["report_digest"],
            "report_expires_at": plan["report_expires_at"],
            "status": plan["status"],
            "source_date": report["source_date"],
            "candidate_range": report["candidate_range"],
            "selected_date": report["selected_date"],
            "feasible": report["feasible"],
            "generated_at": report["generated_at"],
            "totals": report["totals"],
            "conflicts": report["conflicts"],
            "date_evaluations": report["date_evaluations"],
        }
        if full:
            summary["affected_orders"] = report["affected_orders"]
            summary["resource_migrations"] = report["resource_migrations"]
            summary["fee_differences"] = report["fee_differences"]
            summary["input_digest"] = report["input_digest"]
        return summary

    def list_confirmations(self, plan_id: int) -> list[dict[str, Any]]:
        self._require_plan(self.connection, plan_id)
        rows = self.connection.execute("SELECT * FROM reschedule_confirmations WHERE plan_id=? ORDER BY id", (plan_id,)).fetchall()
        return [dict(row) for row in rows]

    def final_version(self, plan_id: int, *, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        connection = connection or self.connection
        self._require_plan(connection, plan_id)
        row = connection.execute("SELECT * FROM reschedule_results WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFoundError("计划尚未完成切换，没有最终版本")
        result = dict(row)
        result["detail"] = json.loads(result.pop("detail_json"))
        return result

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _validate_window(self, payload: dict[str, Any]) -> None:
        source = date.fromisoformat(payload["source_date"])
        start = date.fromisoformat(payload["candidate_start"])
        end = date.fromisoformat(payload["candidate_end"])
        if end < start:
            raise ValidationError("候选日期范围结束日不能早于开始日")
        if (end - start).days >= MAX_CANDIDATE_DAYS:
            raise ValidationError(f"候选日期范围不能超过 {MAX_CANDIDATE_DAYS} 天")
        if start <= source <= end:
            raise ValidationError("候选日期范围不能包含原日期")

    def _require_plan(self, connection: sqlite3.Connection, plan_id: int) -> sqlite3.Row:
        plan = connection.execute("SELECT * FROM reschedule_plans WHERE id=?", (plan_id,)).fetchone()
        if plan is None:
            raise NotFoundError("改期计划不存在")
        return plan

    def _refresh_expiry(self, plan_id: int) -> None:
        """惰性过期：在独立事务中提交过期转换，调用方随后的校验失败不会将其回滚。"""
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            plan = self._require_plan(connection, plan_id)
            self._expire_if_needed(connection, plan, now)

    def _mark_failed(self, plan_id: int, actor: str, reason: str, now: str, *, from_status: str) -> None:
        """安全失败：仅在计划仍停留在预期中间态时落库，原因持久保留。"""
        action = {"applying": "apply_failed", "rehearsing": "rehearsal_failed"}.get(from_status, "failed")
        with transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE reschedule_plans SET status='failed',failure_reason=?,updated_at=?,version=version+1 WHERE id=? AND status=?",
                (reason, now, plan_id, from_status),
            )
            self._add_event(connection, plan_id, action, actor, {"reason": reason}, now)

    def _expire_if_needed(self, connection: sqlite3.Connection, plan: sqlite3.Row, now: str) -> None:
        if plan["status"] in {"report_ready", "approved"} and plan["report_expires_at"] and plan["report_expires_at"] <= now:
            connection.execute(
                "UPDATE reschedule_plans SET status='expired',failure_reason='报告已过期，请重新预演',updated_at=?,version=version+1 WHERE id=?",
                (now, plan["id"]),
            )
            self._add_event(connection, plan["id"], "expired", "system", {"report_version": plan["report_version"], "expired_at": plan["report_expires_at"]}, now)

    def _affected_tasks(self, connection: sqlite3.Connection, source_date: str) -> list[sqlite3.Row]:
        placeholders = ",".join("?" for _ in AFFECTED_TASK_STATUSES)
        return connection.execute(
            f"SELECT t.*,tpl.code AS template_code FROM compute_tasks t JOIN compute_templates tpl ON tpl.id=t.template_id "
            f"WHERE substr(t.available_at,1,10)=? AND t.status IN ({placeholders}) ORDER BY t.id",
            (source_date, *AFFECTED_TASK_STATUSES),
        ).fetchall()

    @staticmethod
    def _bookings_of(connection: sqlite3.Connection, task_ids: list[int]) -> list[sqlite3.Row]:
        if not task_ids:
            return []
        placeholders = ",".join("?" for _ in task_ids)
        return connection.execute(
            f"SELECT * FROM ceremony_bookings WHERE task_id IN ({placeholders}) ORDER BY id",
            task_ids,
        ).fetchall()

    @staticmethod
    def _resources_by_id(connection: sqlite3.Connection, resource_ids: list[int]) -> dict[int, sqlite3.Row]:
        if not resource_ids:
            return {}
        placeholders = ",".join("?" for _ in resource_ids)
        rows = connection.execute(f"SELECT * FROM ceremony_resources WHERE id IN ({placeholders})", resource_ids).fetchall()
        return {int(row["id"]): row for row in rows}

    @staticmethod
    def _usage_on_dates(connection: sqlite3.Connection, dates: list[str], resource_ids: list[int], *, exclude_task_ids: list[int]) -> dict[tuple[int, str], int]:
        if not dates or not resource_ids:
            return {}
        date_placeholders = ",".join("?" for _ in dates)
        resource_placeholders = ",".join("?" for _ in resource_ids)
        exclusion = ""
        params: list[Any] = list(dates) + list(resource_ids)
        if exclude_task_ids:
            exclusion = " AND task_id NOT IN (" + ",".join("?" for _ in exclude_task_ids) + ")"
            params.extend(exclude_task_ids)
        rows = connection.execute(
            f"SELECT resource_id,booking_date,SUM(units) AS used FROM ceremony_bookings WHERE booking_date IN ({date_placeholders}) AND resource_id IN ({resource_placeholders}){exclusion} GROUP BY resource_id,booking_date",
            params,
        ).fetchall()
        return {(int(row["resource_id"]), str(row["booking_date"])): int(row["used"]) for row in rows}

    def _current_input_digest(self, connection: sqlite3.Connection, plan: sqlite3.Row) -> str:
        tasks = self._affected_tasks(connection, plan["source_date"])
        task_ids = [int(task["id"]) for task in tasks]
        bookings = self._bookings_of(connection, task_ids)
        resource_ids = sorted({int(booking["resource_id"]) for booking in bookings})
        resources = self._resources_by_id(connection, resource_ids)
        candidate_days = _date_range(date.fromisoformat(plan["candidate_start"]), date.fromisoformat(plan["candidate_end"]))
        usage = self._usage_on_dates(connection, [day.isoformat() for day in candidate_days], resource_ids, exclude_task_ids=task_ids)
        return _digest(
            {
                "source_date": plan["source_date"],
                "candidate_start": plan["candidate_start"],
                "candidate_end": plan["candidate_end"],
                "tasks": [[int(t["id"]), t["status"], t["available_at"], int(t["version"])] for t in tasks],
                "bookings": [
                    [int(b["id"]), int(b["task_id"]), int(b["resource_id"]), b["booking_date"], int(b["units"]), b["commitment"], int(b["fee_cents"])]
                    for b in bookings
                ],
                "resources": [[rid, int(resources[rid]["daily_capacity"]), int(resources[rid]["active"]), int(resources[rid]["change_fee_cents"])] for rid in resource_ids],
                "usage": [[rid, day, int(amount)] for (rid, day), amount in sorted(usage.items())],
            }
        )

    def _plan_view(self, connection: sqlite3.Connection, plan_id: int) -> dict[str, Any]:
        plan = self._require_plan(connection, plan_id)
        return self._plan_summary(dict(plan))

    @staticmethod
    def _plan_summary(plan: dict[str, Any]) -> dict[str, Any]:
        report = json.loads(plan["report_json"]) if plan["report_json"] else {}
        return {
            "id": plan["id"],
            "requested_by": plan["requested_by"],
            "source_date": plan["source_date"],
            "candidate_start": plan["candidate_start"],
            "candidate_end": plan["candidate_end"],
            "reason": plan["reason"],
            "status": plan["status"],
            "report_version": plan["report_version"],
            "report_digest": plan["report_digest"],
            "report_expires_at": plan["report_expires_at"],
            "failure_reason": plan["failure_reason"],
            "applied_at": plan["applied_at"],
            "version": plan["version"],
            "created_at": plan["created_at"],
            "updated_at": plan["updated_at"],
            "report_totals": report.get("totals"),
            "selected_date": report.get("selected_date"),
            "feasible": report.get("feasible"),
        }

    @staticmethod
    def _confirmations(connection: sqlite3.Connection, plan_id: int, report_version: int) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM reschedule_confirmations WHERE plan_id=? AND report_version=? ORDER BY id",
            (plan_id, report_version),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _add_event(connection: sqlite3.Connection, plan_id: int, action: str, actor: str, detail: dict[str, Any], now: str) -> None:
        connection.execute(
            "INSERT INTO reschedule_events(plan_id,action,actor,detail_json,created_at) VALUES(?,?,?,?,?)",
            (plan_id, action, actor, _dump(detail), now),
        )
