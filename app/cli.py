from __future__ import annotations

import argparse
import json

from fastapi.testclient import TestClient

from app.database import database_path, get_connection, init_db
from app.main import app


def command_init() -> int:
    init_db()
    print(json.dumps({"database": str(database_path()), "status": "initialized"}, ensure_ascii=False))
    return 0


def command_check() -> int:
    init_db()
    connection = get_connection()
    result = {
        "database": str(database_path()),
        "integrity": connection.execute("PRAGMA integrity_check").fetchone()[0],
        "foreign_keys": connection.execute("PRAGMA foreign_keys").fetchone()[0],
        "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
        "tables": connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0],
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["integrity"] == "ok" and result["foreign_keys"] == 1 else 1


def command_smoke() -> int:
    with TestClient(app) as client:
        root = client.get("/")
        health = client.get("/api/system/health")
    result = {"root": root.json(), "health": health.json(), "status_codes": [root.status_code, health.status_code]}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status_codes"] == [200, 200] else 1


def command_compute_demo() -> int:
    template = {
        "code": "monte-carlo-demo",
        "name": "蒙特卡洛演示",
        "algorithm": "monte-carlo",
        "parameter_schema": {
            "samples": {"type": "integer", "required": True, "minimum": 10, "maximum": 1000000},
            "seed": {"type": "integer", "required": True},
        },
        "default_parameters": {},
        "max_runtime_seconds": 60,
        "max_attempts": 3,
    }
    with TestClient(app) as client:
        created = client.post("/api/compute/templates?actor=cli-demo", json=template)
        if created.status_code not in {201, 409}:
            print(created.text)
            return 1
        task = client.post(
            "/api/compute/tasks",
            json={
                "template_code": "monte-carlo-demo",
                "project_code": "demo",
                "requested_by": "cli-user",
                "parameters": {"samples": 1000, "seed": 42},
                "priority": 80,
                "idempotency_key": "compute-demo-000001",
            },
        )
        claimed = client.post(
            "/api/compute/tasks/claim",
            json={"worker_id": "cli-worker", "capabilities": ["monte-carlo"], "lease_seconds": 60},
        )
    result = {"task": task.status_code, "claimed": claimed.status_code, "task_id": task.json().get("id")}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if task.status_code == 202 and claimed.status_code == 200 and claimed.json().get("task") else 1


def command_reschedule_demo() -> int:
    """演示暴雨改期：预演 → 双人分权确认 → 原子切换。"""
    from datetime import UTC, datetime, timedelta

    run = datetime.now(UTC).strftime("%H%M%S")
    source = (datetime.now(UTC) + timedelta(days=7)).date()
    headers_admin: dict[str, str] = {}
    with TestClient(app) as client:
        bootstrap = client.post("/api/auth/bootstrap", json={"username": "admin", "password": "Admin!23456", "client_label": "cli-demo"})
        if bootstrap.status_code not in {201, 409}:
            print(bootstrap.text)
            return 1
        login = client.post("/api/auth/login", json={"username": "admin", "password": "Admin!23456", "client_label": "cli-demo"})
        if login.status_code != 200:
            print(login.text)
            return 1
        headers_admin = {"Authorization": f"Bearer {login.json()['token']}"}
        actors: dict[str, dict[str, str]] = {}
        for username, role_code, permissions in [
            (f"ops.demo.{run}", "reschedule.demo.ops", ["reschedule.read", "reschedule.confirm.operations"]),
            (f"fin.demo.{run}", "reschedule.demo.fin", ["reschedule.read", "reschedule.confirm.finance"]),
        ]:
            role = client.post("/api/roles", headers=headers_admin, json={"code": role_code, "name": role_code, "permission_codes": permissions})
            if role.status_code not in {201, 409}:
                print(role.text)
                return 1
            user = client.post("/api/users", headers=headers_admin, json={"username": username, "password": "Confirm!23456", "display_name": username, "role_codes": [role_code]})
            if user.status_code != 201:
                print(user.text)
                return 1
            user_login = client.post("/api/auth/login", json={"username": username, "password": "Confirm!23456", "client_label": "cli-demo"})
            actors[role_code] = {"Authorization": f"Bearer {user_login.json()['token']}"}
        template = {
            "code": f"wedding-demo-{run}",
            "name": "婚礼演示套餐",
            "algorithm": "ceremony-demo",
            "parameter_schema": {"guests": {"type": "integer", "required": True, "minimum": 1, "maximum": 2000}},
            "default_parameters": {},
            "max_runtime_seconds": 300,
            "max_attempts": 1,
        }
        created_template = client.post("/api/compute/templates?actor=cli-demo", json=template)
        if created_template.status_code != 201:
            print(created_template.text)
            return 1
        task_ids: list[int] = []
        for index in range(2):
            task = client.post(
                "/api/compute/tasks",
                json={
                    "template_code": template["code"],
                    "project_code": "hall-demo",
                    "requested_by": "cli-family",
                    "parameters": {"guests": 120},
                    "priority": 50,
                    "idempotency_key": f"reschedule-demo-{run}-{index:04d}",
                },
            )
            if task.status_code != 202:
                print(task.text)
                return 1
            task_ids.append(task.json()["id"])
        from app.database import transaction

        with transaction(immediate=True) as connection:
            connection.executemany(
                "UPDATE compute_tasks SET available_at=? WHERE id=?",
                [(f"{source.isoformat()}T09:00:00+00:00", task_id) for task_id in task_ids],
            )
        resource = client.post(
            "/api/reschedule/resources",
            headers=headers_admin,
            json={"resource_type": "vehicle", "code": f"veh-demo-{run}", "name": "礼宾车", "daily_capacity": 2, "change_fee_cents": 5000},
        )
        if resource.status_code != 201:
            print(resource.text)
            return 1
        for task_id in task_ids:
            booking = client.post("/api/reschedule/bookings", headers=headers_admin, json={"task_id": task_id, "resource_code": f"veh-demo-{run}", "units": 1, "fee_cents": 30000})
            if booking.status_code != 201:
                print(booking.text)
                return 1
        plan = client.post(
            "/api/reschedule/plans",
            headers=headers_admin,
            json={
                "source_date": source.isoformat(),
                "candidate_start": (source + timedelta(days=1)).isoformat(),
                "candidate_end": (source + timedelta(days=3)).isoformat(),
                "reason": "暴雨红色预警，整体改期",
                "report_ttl_minutes": 30,
                "idempotency_key": f"reschedule-demo-{run}",
            },
        )
        if plan.status_code != 201:
            print(plan.text)
            return 1
        plan_id = plan.json()["id"]
        confirm_ops = client.post(f"/api/reschedule/plans/{plan_id}/confirm", headers=actors["reschedule.demo.ops"], json={"domain": "operations", "report_version": 1, "note": "人员车辆可迁移"})
        confirm_fin = client.post(f"/api/reschedule/plans/{plan_id}/confirm", headers=actors["reschedule.demo.fin"], json={"domain": "finance", "report_version": 1, "note": "费用差异已复核"})
        applied = client.post(f"/api/reschedule/plans/{plan_id}/apply", headers=headers_admin)
        final = client.get(f"/api/reschedule/plans/{plan_id}/final", headers=headers_admin)
    result = {
        "plan_id": plan_id,
        "rehearsal": plan.json()["status"],
        "selected_date": plan.json()["selected_date"],
        "confirmations": [confirm_ops.status_code, confirm_fin.status_code],
        "apply": applied.status_code,
        "final": final.json() if final.status_code == 200 else final.text,
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if confirm_ops.status_code == confirm_fin.status_code == 201 and applied.status_code == 200 else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="ceremony-operations", description="红白喜事服务运营平台维护入口")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="初始化 SQLite 数据库")
    subparsers.add_parser("check-db", help="检查数据库完整性")
    subparsers.add_parser("smoke", help="执行本地 API 冒烟检查")
    subparsers.add_parser("compute-demo", help="执行计算任务提交与领取演示")
    subparsers.add_parser("reschedule-demo", help="执行暴雨批量改期预演、双人确认与原子切换演示")
    args = parser.parse_args()
    return {"init-db": command_init, "check-db": command_check, "smoke": command_smoke, "compute-demo": command_compute_demo, "reschedule-demo": command_reschedule_demo}[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
