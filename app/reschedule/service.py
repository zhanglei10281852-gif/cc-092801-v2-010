from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import date as date_type
from datetime import timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, DomainError, NotFoundError, ValidationError
from app.core.security import Principal
from app.database import get_connection, transaction
from app.reschedule.repository import ACTIVE_ORDER_STATUSES, RescheduleRepository, slot_name, venue_slot

REPORT_TTL_SECONDS = 1800
SERVICE_FEE_RATE_PERCENT = 5
PENALTY_RATE_PERCENT = 20
REQUIRED_GROUPS = ("operations", "finance")
GROUP_PERMISSIONS = {
    "operations": "reschedule.confirm.operations",
    "finance": "reschedule.confirm.finance",
}
GROUP_LABELS = {"operations": "运营", "finance": "财务"}
PRICING_RULES = {
    "order_service_fee_percent": SERVICE_FEE_RATE_PERCENT,
    "broken_commitment_penalty_percent": PENALTY_RATE_PERCENT,
}


def digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def _date_text(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _as_date(value: Any) -> date_type:
    return value if isinstance(value, date_type) else date_type.fromisoformat(str(value))


class RescheduleService:
    """批量改期：先预演生成报告，双人确认后原子切换，失败安全并保留原因。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()

    # ---- 仪式订单 ----

    def create_order(self, payload: dict[str, Any], principal: Principal) -> dict[str, Any]:
        principal.require("reschedule.rehearse")
        resources = payload.get("resources") or []
        seen = {(item["resource_type"], item["resource_name"]) for item in resources}
        if len(seen) != len(resources):
            raise ValidationError("同一订单中资源不能重复")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = RescheduleRepository(connection)
            if repository.order_by_code(payload["order_code"]):
                raise ConflictError("订单编号已存在")
            order = repository.create_order(
                order_code=payload["order_code"],
                ceremony_type=payload["ceremony_type"],
                ceremony_date=_date_text(payload["ceremony_date"]),
                venue=payload["venue"],
                customer_name=payload["customer_name"],
                base_fee_cents=int(payload["base_fee_cents"]),
                now=now,
            )
            for item in resources:
                repository.add_resource(
                    order_id=order["id"],
                    resource_type=item["resource_type"],
                    resource_name=item["resource_name"],
                    commitment_fee_cents=int(item.get("commitment_fee_cents", 0)),
                    now=now,
                )
            return self._order_view(repository, order["id"])

    def list_orders(self, *, date: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        repository = RescheduleRepository(self.connection)
        orders = repository.list_orders(date=date, status=status)
        resources = repository.resources_of([order["id"] for order in orders])
        for order in orders:
            order["resources"] = resources.get(order["id"], [])
        return orders

    @staticmethod
    def _order_view(repository: RescheduleRepository, order_id: int) -> dict[str, Any]:
        order = dict(repository.order_by_id(order_id))
        order["resources"] = repository.resources_of([order_id]).get(order_id, [])
        return order

    # ---- 预演 ----

    def create_rehearsal(self, payload: dict[str, Any], principal: Principal) -> dict[str, Any]:
        principal.require("reschedule.rehearse")
        now_value = self.clock.now()
        now = to_storage(now_value)
        source_date = _date_text(payload["source_date"])
        window_start = _as_date(payload["window_start"])
        window_end = _as_date(payload["window_end"])
        candidates: list[str] = []
        day = window_start
        while day <= window_end:
            candidates.append(day.isoformat())
            day += timedelta(days=1)
        if not candidates:
            raise ValidationError("候选日期范围为空")
        # 第一步：登记预演并立即提交，使进度可查询；若服务在此后崩溃，恢复流程会安全终止它。
        with transaction(immediate=True) as connection:
            repository = RescheduleRepository(connection)
            rehearsal = repository.create_rehearsal(
                rehearsal_code=f"RS-{uuid.uuid4().hex[:12].upper()}",
                source_date=source_date,
                window_start=candidates[0],
                window_end=candidates[-1],
                request_reason=payload["reason"],
                requested_by=principal.username,
                now=now,
            )
            rehearsal_id = int(rehearsal["id"])
            repository.insert_event(
                rehearsal_id=rehearsal_id, action="created", actor=principal.username,
                reason=payload["reason"],
                details={"source_date": source_date, "candidates": candidates}, now=now,
            )
        # 第二步：只读分析现网数据并生成报告，不改动任何生效订单。
        with transaction(immediate=True) as connection:
            repository = RescheduleRepository(connection)
            analysis = self._analyze(repository, source_date, candidates)
            if analysis["failure"]:
                repository.finish_analysis(
                    rehearsal_id, status="failed", target_date=None, report_version=0,
                    report_digest="", expires_at=None, failure_reason=analysis["failure"], now=now,
                )
                repository.insert_event(
                    rehearsal_id=rehearsal_id, action="analysis_failed", actor=principal.username,
                    reason=analysis["failure"], details=None, now=now,
                )
            else:
                report_version = 1
                report_digest = digest({
                    "version": report_version, "source_date": source_date,
                    "candidates": candidates, "pricing_rules": PRICING_RULES,
                    "affected_orders": analysis["affected"], "evaluations": analysis["evaluations"],
                    "recommended": analysis["recommended"],
                })
                recommended = analysis["recommended"]
                repository.insert_report(
                    rehearsal_id=rehearsal_id, version=report_version,
                    target_date=recommended["date"] if recommended else None,
                    summary=analysis["summary"], details=analysis["details"],
                    report_digest=report_digest, now=now,
                )
                if recommended is not None:
                    expires_at = to_storage(now_value + timedelta(seconds=REPORT_TTL_SECONDS))
                    repository.finish_analysis(
                        rehearsal_id, status="awaiting_confirmation", target_date=recommended["date"],
                        report_version=report_version, report_digest=report_digest,
                        expires_at=expires_at, failure_reason="", now=now,
                    )
                    repository.insert_event(
                        rehearsal_id=rehearsal_id, action="analyzed", actor=principal.username,
                        reason="预演完成，等待双人确认",
                        details={"target_date": recommended["date"], "expires_at": expires_at}, now=now,
                    )
                else:
                    repository.finish_analysis(
                        rehearsal_id, status="blocked", target_date=None,
                        report_version=report_version, report_digest=report_digest,
                        expires_at=None, failure_reason=analysis["block_reason"], now=now,
                    )
                    repository.insert_event(
                        rehearsal_id=rehearsal_id, action="blocked", actor=principal.username,
                        reason=analysis["block_reason"], details=None, now=now,
                    )
        return self.get_rehearsal(rehearsal_id)

    def _analyze(self, repository: RescheduleRepository, source_date: str, candidates: list[str]) -> dict[str, Any]:
        orders = repository.orders_on_date(source_date)
        if not orders:
            return {"failure": f"原定日期 {source_date} 没有可改期的有效订单"}
        order_ids = [int(order["id"]) for order in orders]
        resources = repository.resources_of(order_ids)
        affected: list[dict[str, Any]] = []
        for order in orders:
            affected.append({
                "order_id": order["id"],
                "order_code": order["order_code"],
                "ceremony_type": order["ceremony_type"],
                "customer_name": order["customer_name"],
                "venue": order["venue"],
                "base_fee_cents": order["base_fee_cents"],
                "resources": [
                    {"resource_type": item["resource_type"], "resource_name": item["resource_name"], "commitment_fee_cents": item["commitment_fee_cents"]}
                    for item in resources.get(order["id"], [])
                ],
            })
        evaluations: list[dict[str, Any]] = []
        for candidate in candidates:
            occupied = repository.occupied_slots(candidate, exclude_order_ids=order_ids)
            order_plans: list[dict[str, Any]] = []
            conflicts: list[dict[str, Any]] = []
            migratable_count = 0
            unmigratable_count = 0
            total_delta = 0
            for order in affected:
                moves: list[dict[str, Any]] = []
                venue_ok = venue_slot(order["venue"]) not in occupied
                moves.append({"resource_type": "venue", "resource_name": order["venue"], "commitment_fee_cents": 0, "migratable": venue_ok})
                penalty = 0
                if venue_ok:
                    migratable_count += 1
                else:
                    unmigratable_count += 1
                    conflicts.append({"order_code": order["order_code"], "resource_type": "venue", "resource_name": order["venue"]})
                for resource in order["resources"]:
                    ok = slot_name(resource["resource_type"], resource["resource_name"]) not in occupied
                    moves.append({**resource, "migratable": ok})
                    if ok:
                        migratable_count += 1
                    else:
                        unmigratable_count += 1
                        penalty += resource["commitment_fee_cents"] * PENALTY_RATE_PERCENT // 100
                        conflicts.append({"order_code": order["order_code"], "resource_type": resource["resource_type"], "resource_name": resource["resource_name"]})
                service_fee = order["base_fee_cents"] * SERVICE_FEE_RATE_PERCENT // 100
                order_plans.append({
                    "order_id": order["order_id"],
                    "order_code": order["order_code"],
                    "service_fee_cents": service_fee,
                    "penalty_cents": penalty,
                    "fee_delta_cents": service_fee + penalty,
                    "moves": moves,
                })
                total_delta += service_fee + penalty
            evaluations.append({
                "date": candidate,
                "feasible": unmigratable_count == 0,
                "conflict_count": len(conflicts),
                "migratable_resource_count": migratable_count,
                "unmigratable_resource_count": unmigratable_count,
                "fee_delta_cents": total_delta,
                "conflicts": conflicts,
                "order_plans": order_plans,
            })
        feasible = [item for item in evaluations if item["feasible"]]
        recommended = min(feasible, key=lambda item: (item["fee_delta_cents"], item["date"])) if feasible else None
        block_reason = "" if recommended else "所有候选日期均存在资源冲突，无法整体改期"
        summary = {
            "source_date": source_date,
            "window": {"start": candidates[0], "end": candidates[-1]},
            "affected_order_count": len(affected),
            "candidate_count": len(evaluations),
            "feasible_candidate_count": len(feasible),
            "recommended_target_date": recommended["date"] if recommended else None,
            "total_fee_delta_cents": recommended["fee_delta_cents"] if recommended else None,
            "pricing_rules": PRICING_RULES,
            "candidates": [
                {key: item[key] for key in ("date", "feasible", "conflict_count", "migratable_resource_count", "unmigratable_resource_count", "fee_delta_cents")}
                for item in evaluations
            ],
        }
        details = {
            "source_date": source_date,
            "pricing_rules": PRICING_RULES,
            "affected_orders": affected,
            "evaluations": evaluations,
            "recommended": recommended,
        }
        return {
            "failure": "",
            "affected": affected,
            "evaluations": evaluations,
            "recommended": recommended,
            "block_reason": block_reason,
            "summary": summary,
            "details": details,
        }

    # ---- 查询 ----

    def get_rehearsal(self, rehearsal_id: int) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = RescheduleRepository(connection)
            rehearsal = repository.rehearsal_by_id(rehearsal_id)
            if rehearsal is None:
                raise NotFoundError("改期预演不存在")
            if self._expire_if_due(repository, dict(rehearsal), now, actor="system"):
                rehearsal = repository.rehearsal_by_id(rehearsal_id)
            return self._progress_view(repository, dict(rehearsal))

    def list_rehearsals(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = RescheduleRepository(connection)
            rows = repository.list_rehearsals(status=status, limit=max(1, min(limit, 500)))
            return [self._progress_view(repository, row) for row in rows]

    def get_report(self, rehearsal_id: int) -> dict[str, Any]:
        repository = RescheduleRepository(self.connection)
        if repository.rehearsal_by_id(rehearsal_id) is None:
            raise NotFoundError("改期预演不存在")
        report = repository.latest_report(rehearsal_id)
        if report is None:
            raise NotFoundError("预演尚未生成报告")
        return {
            "rehearsal_id": rehearsal_id,
            "version": report["version"],
            "target_date": report["target_date"],
            "report_digest": report["report_digest"],
            "generated_at": report["generated_at"],
            "summary": json.loads(report["summary_json"]),
            "details": json.loads(report["details_json"]),
        }

    def list_confirmations(self, rehearsal_id: int) -> list[dict[str, Any]]:
        repository = RescheduleRepository(self.connection)
        if repository.rehearsal_by_id(rehearsal_id) is None:
            raise NotFoundError("改期预演不存在")
        return repository.confirmations_for(rehearsal_id)

    def list_events(self, rehearsal_id: int) -> list[dict[str, Any]]:
        repository = RescheduleRepository(self.connection)
        if repository.rehearsal_by_id(rehearsal_id) is None:
            raise NotFoundError("改期预演不存在")
        return repository.events_for(rehearsal_id)

    def get_result(self, rehearsal_id: int) -> dict[str, Any]:
        repository = RescheduleRepository(self.connection)
        rehearsal = repository.rehearsal_by_id(rehearsal_id)
        if rehearsal is None:
            raise NotFoundError("改期预演不存在")
        if rehearsal["status"] != "applied":
            raise ConflictError("预演尚未完成切换，没有最终版本", context={"status": rehearsal["status"]})
        moves = repository.moves_for(rehearsal_id)
        for move in moves:
            move["moved_resources"] = json.loads(move.pop("moved_resources_json"))
        return {
            "rehearsal_id": rehearsal_id,
            "rehearsal_code": rehearsal["rehearsal_code"],
            "status": rehearsal["status"],
            "report_version": rehearsal["report_version"],
            "report_digest": rehearsal["report_digest"],
            "source_date": rehearsal["source_date"],
            "target_date": rehearsal["target_date"],
            "applied_at": rehearsal["applied_at"],
            "total_fee_delta_cents": sum(int(move["fee_delta_cents"]) for move in moves),
            "moves": moves,
        }

    # ---- 确认与原子切换 ----

    def confirm(self, rehearsal_id: int, principal: Principal, group: str, report_version: int | None, note: str) -> dict[str, Any]:
        principal.require(GROUP_PERMISSIONS[group])
        now = to_storage(self.clock.now())
        failure: DomainError | None = None
        response: dict[str, Any] = {}
        with transaction(immediate=True) as connection:
            repository = RescheduleRepository(connection)
            row = repository.rehearsal_by_id(rehearsal_id)
            if row is None:
                failure = NotFoundError("改期预演不存在")
            else:
                rehearsal = dict(row)
                if self._expire_if_due(repository, rehearsal, now, actor=principal.username):
                    failure = ConflictError("报告已过期，预演已安全终止", context={"rehearsal_id": rehearsal_id})
                elif rehearsal["status"] != "awaiting_confirmation":
                    reason = f"预演当前状态为 {rehearsal['status']}，不能接受确认"
                    repository.insert_event(rehearsal_id=rehearsal_id, action="confirmation_rejected", actor=principal.username, reason=reason, details={"group": group}, now=now)
                    failure = ConflictError(reason, context={"rehearsal_id": rehearsal_id, "status": rehearsal["status"]})
                elif report_version is not None and report_version != int(rehearsal["report_version"]):
                    reason = "确认的报告版本与当前报告不一致"
                    repository.insert_event(rehearsal_id=rehearsal_id, action="confirmation_rejected", actor=principal.username, reason=reason, details={"group": group, "report_version": report_version}, now=now)
                    failure = ConflictError("报告版本不一致，请复核最新报告后再确认", context={"current_version": rehearsal["report_version"]})
                else:
                    confirmations = repository.confirmations_for(rehearsal_id, int(rehearsal["report_version"]))
                    if any(item["actor"] == principal.username for item in confirmations):
                        reason = "同一人员重复确认同一份报告"
                        repository.insert_event(rehearsal_id=rehearsal_id, action="confirmation_rejected", actor=principal.username, reason=reason, details={"group": group}, now=now)
                        failure = ConflictError("该人员已确认过此报告，重复确认不被接受", context={"rehearsal_id": rehearsal_id})
                    elif any(item["permission_group"] == group for item in confirmations):
                        reason = f"{GROUP_LABELS[group]}权限组重复确认同一份报告"
                        repository.insert_event(rehearsal_id=rehearsal_id, action="confirmation_rejected", actor=principal.username, reason=reason, details={"group": group}, now=now)
                        failure = ConflictError(f"{GROUP_LABELS[group]}权限组已完成确认，重复确认不被接受", context={"rehearsal_id": rehearsal_id})
                    else:
                        confirmation = repository.insert_confirmation(
                            rehearsal_id=rehearsal_id, report_version=int(rehearsal["report_version"]),
                            report_digest=rehearsal["report_digest"], permission_group=group,
                            actor=principal.username, actor_name=principal.display_name, note=note, now=now,
                        )
                        repository.insert_event(rehearsal_id=rehearsal_id, action="confirmed", actor=principal.username, reason=note or f"{GROUP_LABELS[group]}确认", details={"group": group, "report_version": rehearsal["report_version"]}, now=now)
                        confirmations = repository.confirmations_for(rehearsal_id, int(rehearsal["report_version"]))
                        confirmed_groups = {item["permission_group"] for item in confirmations}
                        response = {"confirmation": confirmation, "applied": False}
                        if all(item in confirmed_groups for item in REQUIRED_GROUPS):
                            apply_failure = self._apply(repository, rehearsal, now)
                            if apply_failure is not None:
                                repository.mark_terminal(rehearsal_id, status="failed", failure_reason=apply_failure, now=now)
                                repository.insert_event(rehearsal_id=rehearsal_id, action="apply_failed", actor=principal.username, reason=apply_failure, details=None, now=now)
                                failure = ConflictError(f"双人确认完成，但切换未执行：{apply_failure}", context={"rehearsal_id": rehearsal_id})
                            else:
                                repository.insert_event(rehearsal_id=rehearsal_id, action="applied", actor=principal.username, reason="双人确认完成，已原子切换", details={"target_date": rehearsal["target_date"]}, now=now)
                                response["applied"] = True
        if failure is not None:
            raise failure
        response["rehearsal"] = self.get_rehearsal(rehearsal_id)
        if response.get("applied"):
            response["result"] = self.get_result(rehearsal_id)
        return response

    def _apply(self, repository: RescheduleRepository, rehearsal: dict[str, Any], now: str) -> str | None:
        """在确认事务内复核并执行原子切换；返回失败原因（未改动任何订单）或 None。"""
        report = repository.latest_report(int(rehearsal["id"]))
        if report is None or int(report["version"]) != int(rehearsal["report_version"]) or report["report_digest"] != rehearsal["report_digest"]:
            return "报告内容已变化，无法安全切换"
        details = json.loads(report["details_json"])
        recommended = details.get("recommended")
        if not recommended:
            return "报告没有可执行的推荐日期"
        target_date = recommended["date"]
        plans = recommended["order_plans"]
        order_ids = [int(plan["order_id"]) for plan in plans]
        for plan in plans:
            order = repository.order_by_id(int(plan["order_id"]))
            if order is None or order["ceremony_date"] != rehearsal["source_date"] or order["status"] not in ACTIVE_ORDER_STATUSES:
                return f"订单 {plan['order_code']} 在预演后状态已变化，无法安全切换"
        occupied = repository.occupied_slots(target_date, exclude_order_ids=order_ids)
        unavailable: list[str] = []
        for plan in plans:
            for move in plan["moves"]:
                if move["migratable"] and slot_name(move["resource_type"], move["resource_name"]) in occupied:
                    unavailable.append(f"{plan['order_code']}:{move['resource_type']}:{move['resource_name']}")
        if unavailable:
            return "部分资源在确认后不可用：" + "、".join(unavailable)
        for plan in plans:
            repository.apply_order_move(order_id=int(plan["order_id"]), target_date=target_date, fee_delta_cents=int(plan["fee_delta_cents"]), now=now)
            repository.insert_move(
                rehearsal_id=int(rehearsal["id"]), order_id=int(plan["order_id"]), order_code=plan["order_code"],
                from_date=rehearsal["source_date"], to_date=target_date,
                fee_delta_cents=int(plan["fee_delta_cents"]),
                moved_resources=[move for move in plan["moves"] if move["migratable"]], now=now,
            )
        repository.mark_applied(int(rehearsal["id"]), now=now)
        return None

    # ---- 视图与状态 ----

    def _expire_if_due(self, repository: RescheduleRepository, rehearsal: dict[str, Any], now: str, *, actor: str) -> bool:
        if rehearsal["status"] == "awaiting_confirmation" and rehearsal["expires_at"] and rehearsal["expires_at"] <= now:
            reason = "报告已过期，未执行任何变更"
            repository.mark_terminal(int(rehearsal["id"]), status="expired", failure_reason=reason, now=now)
            repository.insert_event(rehearsal_id=int(rehearsal["id"]), action="expired", actor=actor, reason=reason, details={"expires_at": rehearsal["expires_at"]}, now=now)
            rehearsal["status"] = "expired"
            rehearsal["failure_reason"] = reason
            return True
        return False

    def _progress_view(self, repository: RescheduleRepository, rehearsal: dict[str, Any]) -> dict[str, Any]:
        confirmations = repository.confirmations_for(int(rehearsal["id"]), int(rehearsal["report_version"])) if rehearsal["report_version"] else []
        received = sorted({item["permission_group"] for item in confirmations})
        return {
            "id": rehearsal["id"],
            "rehearsal_code": rehearsal["rehearsal_code"],
            "status": rehearsal["status"],
            "source_date": rehearsal["source_date"],
            "window": {"start": rehearsal["window_start"], "end": rehearsal["window_end"]},
            "target_date": rehearsal["target_date"],
            "report_version": rehearsal["report_version"],
            "report_digest": rehearsal["report_digest"],
            "request_reason": rehearsal["request_reason"],
            "requested_by": rehearsal["requested_by"],
            "failure_reason": rehearsal["failure_reason"],
            "progress": {
                "stage": rehearsal["status"],
                "confirmations_received": received,
                "confirmations_required": list(REQUIRED_GROUPS),
                "confirmations_complete": all(item in received for item in REQUIRED_GROUPS),
            },
            "expires_at": rehearsal["expires_at"],
            "applied_at": rehearsal["applied_at"],
            "created_at": rehearsal["created_at"],
            "updated_at": rehearsal["updated_at"],
        }


def recover_incomplete_rehearsals(clock: Clock | None = None) -> dict[str, Any]:
    """服务启动时安全终止未完成的预演：分析中的标记失败、过期报告标记过期，绝不视为已执行。"""
    clock = clock or SystemClock()
    now = to_storage(clock.now())
    failed: list[int] = []
    expired: list[int] = []
    with transaction(immediate=True) as connection:
        repository = RescheduleRepository(connection)
        for row in repository.rehearsals_in_status("analyzing"):
            reason = "服务重启时预演分析未完成，未执行任何变更"
            repository.mark_terminal(int(row["id"]), status="failed", failure_reason=reason, now=now)
            repository.insert_event(rehearsal_id=int(row["id"]), action="restart_recovered", actor="system", reason=reason, details=None, now=now)
            failed.append(int(row["id"]))
        for row in repository.expired_awaiting(now):
            reason = "报告已过期，未执行任何变更"
            repository.mark_terminal(int(row["id"]), status="expired", failure_reason=reason, now=now)
            repository.insert_event(rehearsal_id=int(row["id"]), action="expired", actor="system", reason="服务重启时发现报告已超过确认时限", details={"expires_at": row["expires_at"]}, now=now)
            expired.append(int(row["id"]))
    return {"failed": failed, "expired": expired}
