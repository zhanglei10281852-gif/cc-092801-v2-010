from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.clock import FrozenClock
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.database import get_connection, transaction
from app.reschedule.service import RescheduleService, reconcile_interrupted

SOURCE_DATE = "2026-10-10"
SOURCE_TS = "2026-10-10T08:00:00+00:00"
CANDIDATES = {"candidate_start": "2026-10-11", "candidate_end": "2026-10-13"}

TEMPLATE = {
    "code": "wedding-basic",
    "name": "婚礼基础套餐",
    "algorithm": "ceremony",
    "parameter_schema": {"guests": {"type": "integer", "required": True, "minimum": 1, "maximum": 2000}},
    "default_parameters": {},
    "max_runtime_seconds": 300,
    "max_attempts": 1,
}


def _make_actor(client, admin, username: str, role_code: str, permissions: list[str]) -> dict:
    role = client.post("/api/roles", headers=admin["headers"], json={"code": role_code, "name": role_code, "permission_codes": permissions})
    assert role.status_code == 201, role.text
    user = client.post(
        "/api/users",
        headers=admin["headers"],
        json={"username": username, "password": "Confirm!23456", "display_name": username, "role_codes": [role_code]},
    )
    assert user.status_code == 201, user.text
    login = client.post("/api/auth/login", json={"username": username, "password": "Confirm!23456", "client_label": "tests"})
    assert login.status_code == 200, login.text
    return {"headers": {"Authorization": f"Bearer {login.json()['token']}"}}


def _create_task(client, key: str, guests: int = 100) -> dict:
    response = client.post(
        "/api/compute/tasks",
        json={
            "template_code": "wedding-basic",
            "project_code": "hall-a",
            "requested_by": "family-1",
            "parameters": {"guests": guests},
            "priority": 50,
            "idempotency_key": key,
        },
    )
    assert response.status_code == 202, response.text
    return response.json()


def _schedule_task(task_id: int, when: str = SOURCE_TS) -> None:
    with transaction(immediate=True) as connection:
        connection.execute("UPDATE compute_tasks SET available_at=? WHERE id=?", (when, task_id))


def _setup_ceremony(client, admin, *, task_count: int = 2) -> list[dict]:
    template = client.post("/api/compute/templates?actor=administrator", json=TEMPLATE)
    assert template.status_code == 201, template.text
    tasks = [_create_task(client, f"ceremony-{index:06d}") for index in range(task_count)]
    for task in tasks:
        _schedule_task(task["id"])
    resources = [
        {"resource_type": "staff", "code": "staff-emcee", "name": "司仪", "daily_capacity": 2, "change_fee_cents": 1000},
        {"resource_type": "vehicle", "code": "veh-01", "name": "礼宾车", "daily_capacity": 1, "change_fee_cents": 5000},
        {"resource_type": "supplier", "code": "sup-flowers", "name": "鲜花供应商", "daily_capacity": 1, "change_fee_cents": 20000},
    ]
    for resource in resources:
        created = client.post("/api/reschedule/resources", headers=admin["headers"], json=resource)
        assert created.status_code == 201, created.text
    bookings = [
        {"task_id": tasks[0]["id"], "resource_code": "staff-emcee", "units": 1},
        {"task_id": tasks[0]["id"], "resource_code": "veh-01", "units": 1},
        {"task_id": tasks[0]["id"], "resource_code": "sup-flowers", "units": 1, "commitment": "confirmed", "fee_cents": 100000},
    ]
    if task_count > 1:
        bookings.append({"task_id": tasks[1]["id"], "resource_code": "staff-emcee", "units": 1})
    for booking in bookings:
        created = client.post("/api/reschedule/bookings", headers=admin["headers"], json=booking)
        assert created.status_code == 201, created.text
    return tasks


def _create_plan(client, admin, key: str = "storm-2026-10-0001", **overrides) -> dict:
    payload = {"source_date": SOURCE_DATE, **CANDIDATES, "reason": "暴雨红色预警", "report_ttl_minutes": 30, "idempotency_key": key, **overrides}
    response = client.post("/api/reschedule/plans", headers=admin["headers"], json=payload)
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture()
def actors(client, admin) -> dict:
    return {
        "admin": admin,
        "ops": _make_actor(client, admin, "ops.lead", "reschedule.ops", ["reschedule.read", "reschedule.confirm.operations"]),
        "fin": _make_actor(client, admin, "fin.lead", "reschedule.fin", ["reschedule.read", "reschedule.confirm.finance"]),
    }


def test_full_rehearsal_confirm_apply_flow(client, admin, actors):
    tasks = _setup_ceremony(client, admin)
    plan = _create_plan(client, admin)
    plan_id = plan["id"]
    assert plan["status"] == "report_ready"
    assert plan["report_version"] == 1
    assert plan["feasible"] is True
    assert plan["selected_date"] == "2026-10-11"

    # 预演不改变生效数据
    for task in tasks:
        detail = client.get(f"/api/compute/task-details/{task['id']}").json()
        assert detail["available_at"] == SOURCE_TS
    bookings = client.get("/api/reschedule/bookings", headers=admin["headers"]).json()["items"]
    assert all(item["booking_date"] == SOURCE_DATE for item in bookings)

    progress = client.get(f"/api/reschedule/plans/{plan_id}", headers=admin["headers"]).json()
    assert [event["action"] for event in progress["events"]] == ["created", "report_ready"]

    report = client.get(f"/api/reschedule/plans/{plan_id}/report?full=true", headers=admin["headers"]).json()
    assert report["totals"]["orders"] == 2
    assert report["totals"]["bookings"] == 4
    assert report["totals"]["fee_delta_cents"] == 1000 * 2 + 5000 + 20000
    assert report["totals"]["commitment_resets"] == 1
    assert len(report["affected_orders"]) == 2
    assert len(report["resource_migrations"]) == 4
    assert {item["date"] for item in report["date_evaluations"]} == {"2026-10-11", "2026-10-12", "2026-10-13"}

    first = client.post(f"/api/reschedule/plans/{plan_id}/confirm", headers=actors["ops"]["headers"], json={"domain": "operations", "report_version": 1, "note": "人员车辆可迁移"})
    assert first.status_code == 201, first.text
    assert first.json()["status"] == "report_ready"
    second = client.post(f"/api/reschedule/plans/{plan_id}/confirm", headers=actors["fin"]["headers"], json={"domain": "finance", "report_version": 1, "note": "费用差异可接受"})
    assert second.status_code == 201, second.text
    assert second.json()["status"] == "approved"

    confirmations = client.get(f"/api/reschedule/plans/{plan_id}/confirmations", headers=admin["headers"]).json()["items"]
    assert [(item["domain"], item["actor"]) for item in confirmations] == [("operations", "ops.lead"), ("finance", "fin.lead")]

    applied = client.post(f"/api/reschedule/plans/{plan_id}/apply", headers=admin["headers"])
    assert applied.status_code == 200, applied.text
    result = applied.json()
    assert result["target_date"] == "2026-10-11"
    assert result["moved_tasks"] == 2
    assert result["moved_bookings"] == 4
    assert result["fee_delta_cents"] == 27000
    assert result["commitment_resets"] == 1
    assert result["confirmer_operations"] == "ops.lead"
    assert result["confirmer_finance"] == "fin.lead"

    for task in tasks:
        detail = client.get(f"/api/compute/task-details/{task['id']}").json()
        assert detail["available_at"] == "2026-10-11T08:00:00+00:00"
        assert detail["interventions"][-1]["action"] == "reschedule"
    moved = client.get("/api/reschedule/bookings", headers=admin["headers"]).json()["items"]
    assert all(item["booking_date"] == "2026-10-11" for item in moved)
    supplier = next(item for item in moved if item["resource_code"] == "sup-flowers")
    assert supplier["fee_cents"] == 120000
    assert supplier["commitment"] == "tentative"

    final = client.get(f"/api/reschedule/plans/{plan_id}/final", headers=admin["headers"]).json()
    assert final["report_version"] == 1
    assert final["report_digest"] == plan["report_digest"]
    assert len(final["detail"]["moved_tasks"]) == 2
    state = client.get(f"/api/reschedule/plans/{plan_id}", headers=admin["headers"]).json()
    assert state["status"] == "applied"
    assert [event["action"] for event in state["events"]] == ["created", "report_ready", "confirmed", "confirmed", "approved", "apply_started", "applied"]


def test_plan_idempotency_and_window_validation(client, admin):
    _setup_ceremony(client, admin, task_count=1)
    plan = _create_plan(client, admin)
    replay = _create_plan(client, admin)
    assert replay["id"] == plan["id"]
    conflict = client.post(
        "/api/reschedule/plans",
        headers=admin["headers"],
        json={"source_date": SOURCE_DATE, **CANDIDATES, "reason": "不同的原因", "idempotency_key": "storm-2026-10-0001"},
    )
    assert conflict.status_code == 409
    invalid = client.post(
        "/api/reschedule/plans",
        headers=admin["headers"],
        json={"source_date": SOURCE_DATE, "candidate_start": "2026-10-10", "candidate_end": "2026-10-12", "reason": "窗口包含原日期", "idempotency_key": "storm-2026-10-0002"},
    )
    assert invalid.status_code == 422
    too_wide = client.post(
        "/api/reschedule/plans",
        headers=admin["headers"],
        json={"source_date": SOURCE_DATE, "candidate_start": "2026-10-11", "candidate_end": "2026-11-30", "reason": "窗口过宽", "idempotency_key": "storm-2026-10-0003"},
    )
    assert too_wide.status_code == 422


def test_duplicate_and_same_person_confirmation_rejected(client, admin, actors):
    _setup_ceremony(client, admin, task_count=1)
    plan = _create_plan(client, admin)
    plan_id = plan["id"]
    first = client.post(f"/api/reschedule/plans/{plan_id}/confirm", headers=actors["ops"]["headers"], json={"domain": "operations", "report_version": 1})
    assert first.status_code == 201
    duplicate_domain = client.post(f"/api/reschedule/plans/{plan_id}/confirm", headers=admin["headers"], json={"domain": "operations", "report_version": 1})
    assert duplicate_domain.status_code == 409
    assert "重复确认" in duplicate_domain.json()["error"]["message"]
    admin_ops = client.post(f"/api/reschedule/plans/{plan_id}/confirm", headers=admin["headers"], json={"domain": "finance", "report_version": 1})
    assert admin_ops.status_code == 201
    same_person = client.post(f"/api/reschedule/plans/{plan_id}/confirm", headers=admin["headers"], json={"domain": "operations", "report_version": 1})
    assert same_person.status_code == 409
    progress = client.get(f"/api/reschedule/plans/{plan_id}", headers=admin["headers"]).json()
    rejected = [event for event in progress["events"] if event["action"] == "confirmation_rejected"]
    assert len(rejected) == 2
    assert progress["status"] == "approved"


def test_confirmation_permissions_enforced(client, admin, actors):
    _setup_ceremony(client, admin, task_count=1)
    plan = _create_plan(client, admin)
    plan_id = plan["id"]
    wrong_domain = client.post(f"/api/reschedule/plans/{plan_id}/confirm", headers=actors["ops"]["headers"], json={"domain": "finance", "report_version": 1})
    assert wrong_domain.status_code == 403
    anonymous = client.post(f"/api/reschedule/plans/{plan_id}/confirm", json={"domain": "operations", "report_version": 1})
    assert anonymous.status_code == 401
    outsider = _make_actor(client, admin, "outsider", "reschedule.none", ["residents.read"])
    denied = client.get(f"/api/reschedule/plans/{plan_id}", headers=outsider["headers"])
    assert denied.status_code == 403


def test_infeasible_report_blocks_confirmation(client, admin, actors):
    tasks = _setup_ceremony(client, admin)
    with transaction(immediate=True) as connection:
        connection.execute("UPDATE compute_tasks SET status='running',lease_owner='w1' WHERE id=?", (tasks[0]["id"],))
    plan = _create_plan(client, admin)
    assert plan["feasible"] is False
    assert plan["selected_date"] is None
    report = client.get(f"/api/reschedule/plans/{plan['id']}/report", headers=admin["headers"]).json()
    assert any(item["kind"] == "order_not_movable" for item in report["conflicts"])
    blocked = client.post(f"/api/reschedule/plans/{plan['id']}/confirm", headers=actors["ops"]["headers"], json={"domain": "operations", "report_version": 1})
    assert blocked.status_code == 409
    assert "阻塞冲突" in blocked.json()["error"]["message"]


def test_capacity_conflict_selects_next_feasible_date(client, admin, actors):
    tasks = _setup_ceremony(client, admin, task_count=1)
    other = _create_task(client, "ceremony-other-0001")
    _schedule_task(other["id"], "2026-10-11T09:00:00+00:00")
    booking = client.post("/api/reschedule/bookings", headers=admin["headers"], json={"task_id": other["id"], "resource_code": "veh-01", "units": 1})
    assert booking.status_code == 201
    plan = _create_plan(client, admin)
    assert plan["selected_date"] == "2026-10-12"
    report = client.get(f"/api/reschedule/plans/{plan['id']}/report", headers=admin["headers"]).json()
    evaluations = {item["date"]: item for item in report["date_evaluations"]}
    assert evaluations["2026-10-11"]["feasible"] is False
    assert evaluations["2026-10-11"]["blocking"][0]["kind"] == "resource_unavailable"
    assert evaluations["2026-10-12"]["feasible"] is True


def test_rehearse_new_version_invalidates_old_confirmations(client, admin, actors):
    _setup_ceremony(client, admin, task_count=1)
    plan = _create_plan(client, admin)
    plan_id = plan["id"]
    client.post(f"/api/reschedule/plans/{plan_id}/confirm", headers=actors["ops"]["headers"], json={"domain": "operations", "report_version": 1})
    rehearsed = client.post(f"/api/reschedule/plans/{plan_id}/rehearse", headers=admin["headers"])
    assert rehearsed.status_code == 200, rehearsed.text
    assert rehearsed.json()["report_version"] == 2
    stale = client.post(f"/api/reschedule/plans/{plan_id}/confirm", headers=actors["fin"]["headers"], json={"domain": "finance", "report_version": 1})
    assert stale.status_code == 409
    assert "版本" in stale.json()["error"]["message"]
    ops = client.post(f"/api/reschedule/plans/{plan_id}/confirm", headers=actors["ops"]["headers"], json={"domain": "operations", "report_version": 2})
    assert ops.status_code == 201
    fin = client.post(f"/api/reschedule/plans/{plan_id}/confirm", headers=actors["fin"]["headers"], json={"domain": "finance", "report_version": 2})
    assert fin.status_code == 201
    assert fin.json()["status"] == "approved"
    applied = client.post(f"/api/reschedule/plans/{plan_id}/apply", headers=admin["headers"])
    assert applied.status_code == 200
    assert applied.json()["report_version"] == 2


def _service_setup(clock: FrozenClock) -> tuple[RescheduleService, list[int]]:
    from app.compute.service import ComputeOperationsService

    compute = ComputeOperationsService(get_connection(), clock)
    compute.create_template(TEMPLATE, "administrator")
    task_ids: list[int] = []
    for index in range(2):
        task = compute.submit(
            {
                "template_code": "wedding-basic",
                "project_code": "hall-a",
                "requested_by": "family-1",
                "parameters": {"guests": 100},
                "priority": 50,
                "idempotency_key": f"service-ceremony-{index:06d}",
            }
        )
        task_ids.append(task["id"])
    service = RescheduleService(get_connection(), clock)
    service.create_resource({"resource_type": "staff", "code": "staff-emcee", "name": "司仪", "daily_capacity": 2, "change_fee_cents": 1000}, "administrator")
    service.create_resource({"resource_type": "vehicle", "code": "veh-01", "name": "礼宾车", "daily_capacity": 1, "change_fee_cents": 5000}, "administrator")
    service.create_booking({"task_id": task_ids[0], "resource_code": "veh-01", "units": 1, "commitment": "confirmed", "fee_cents": 8000}, "administrator")
    return service, task_ids


def _approved_plan(service: RescheduleService) -> dict:
    plan = service.create_plan(
        {"source_date": SOURCE_DATE, **CANDIDATES, "reason": "暴雨红色预警", "report_ttl_minutes": 30, "idempotency_key": "service-storm-000001"},
        "scheduler",
    )
    service.confirm(plan["id"], domain="operations", actor="ops.lead", report_version=1, note="", permissions=frozenset({"reschedule.confirm.operations"}))
    service.confirm(plan["id"], domain="finance", actor="fin.lead", report_version=1, note="", permissions=frozenset({"reschedule.confirm.finance"}))
    return service.get_plan(plan["id"])


def test_expired_report_fails_safely(client):
    clock = FrozenClock(datetime(2026, 10, 10, 0, 0, tzinfo=UTC))
    service, _ = _service_setup(clock)
    plan = service.create_plan(
        {"source_date": SOURCE_DATE, **CANDIDATES, "reason": "暴雨红色预警", "report_ttl_minutes": 30, "idempotency_key": "service-storm-000002"},
        "scheduler",
    )
    clock.advance(minutes=31)
    with pytest.raises(ConflictError, match="当前状态不允许确认"):
        service.confirm(plan["id"], domain="operations", actor="ops.lead", report_version=1, note="", permissions=frozenset({"reschedule.confirm.operations"}))
    state = service.get_plan(plan["id"])
    assert state["status"] == "expired"
    assert "过期" in state["failure_reason"]
    with pytest.raises(ConflictError):
        service.apply(plan["id"], "scheduler")
    rehearsed = service.rehearse(plan["id"], "scheduler")
    assert rehearsed["status"] == "report_ready"
    assert rehearsed["report_version"] == 2


def test_restart_leaves_incomplete_rehearsal_failed(client):
    clock = FrozenClock(datetime(2026, 10, 10, 0, 0, tzinfo=UTC))
    service, _ = _service_setup(clock)
    now = "2026-10-10T00:00:00+00:00"
    with transaction(immediate=True) as connection:
        for status in ("rehearsing", "applying"):
            connection.execute(
                "INSERT INTO reschedule_plans(requested_by,idempotency_key,source_date,candidate_start,candidate_end,reason,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                ("scheduler", f"crash-{status}", SOURCE_DATE, "2026-10-11", "2026-10-12", "暴雨预警", status, now, now),
            )
    recovered = reconcile_interrupted(clock)
    assert len(recovered["rehearsing"]) == 1
    assert len(recovered["applying"]) == 1
    plans = service.list_plans(status="failed")
    assert len(plans) == 2
    assert all(plan["failure_reason"] for plan in plans)
    with pytest.raises(NotFoundError):
        service.final_version(plans[0]["id"])
    tasks = get_connection().execute("SELECT available_at FROM compute_tasks").fetchall()
    assert all(row["available_at"].startswith(SOURCE_DATE) for row in tasks)


def test_apply_fails_safely_when_data_drifts(client):
    from app.compute.service import ComputeOperationsService

    clock = FrozenClock(datetime(2026, 10, 10, 0, 0, tzinfo=UTC))
    service, task_ids = _service_setup(clock)
    plan = _approved_plan(service)
    assert plan["status"] == "approved"
    compute = ComputeOperationsService(get_connection(), clock)
    compute.cancel(task_ids[0], "operator", "家庭临时取消")
    with pytest.raises(ConflictError, match="数据已变化"):
        service.apply(plan["id"], "scheduler")
    state = service.get_plan(plan["id"])
    assert state["status"] == "failed"
    assert "数据已变化" in state["failure_reason"]
    survivor = get_connection().execute("SELECT available_at FROM compute_tasks WHERE id=?", (task_ids[1],)).fetchone()
    assert survivor["available_at"].startswith(SOURCE_DATE)


def test_apply_fails_safely_when_resource_shortage(client):
    clock = FrozenClock(datetime(2026, 10, 10, 0, 0, tzinfo=UTC))
    service, task_ids = _service_setup(clock)
    plan = _approved_plan(service)
    with transaction(immediate=True) as connection:
        other = connection.execute(
            "INSERT INTO compute_tasks(template_id,project_code,requested_by,parameters_json,parameter_digest,priority,idempotency_key,status,attempt_count,max_attempts,available_at,created_at,updated_at) "
            "SELECT template_id,'hall-b','family-2','{}','x',50,'competing-000001','queued',0,1,?, ?, ? FROM compute_tasks WHERE id=?",
            ("2026-10-11T08:00:00+00:00", "2026-10-10T00:00:00+00:00", "2026-10-10T00:00:00+00:00", task_ids[0]),
        )
        connection.execute(
            "INSERT INTO ceremony_bookings(task_id,resource_id,booking_date,units,commitment,fee_cents,created_at,updated_at) SELECT ?,resource_id,'2026-10-11',1,'confirmed',0,?,? FROM ceremony_bookings WHERE task_id=?",
            (other.lastrowid, "2026-10-10T00:00:00+00:00", "2026-10-10T00:00:00+00:00", task_ids[0]),
        )
    with pytest.raises(ConflictError, match="部分资源"):
        service.apply(plan["id"], "scheduler")
    state = service.get_plan(plan["id"])
    assert state["status"] == "failed"
    assert "部分资源" in state["failure_reason"]
    unchanged = get_connection().execute("SELECT available_at FROM compute_tasks WHERE id=?", (task_ids[0],)).fetchone()
    assert unchanged["available_at"].startswith(SOURCE_DATE)
    booking = get_connection().execute("SELECT booking_date,fee_cents FROM ceremony_bookings WHERE task_id=?", (task_ids[0],)).fetchone()
    assert booking["booking_date"] == SOURCE_DATE
    assert booking["fee_cents"] == 8000


def test_service_requires_distinct_confirmer_permissions(client):
    clock = FrozenClock(datetime(2026, 10, 10, 0, 0, tzinfo=UTC))
    service, _ = _service_setup(clock)
    plan = service.create_plan(
        {"source_date": SOURCE_DATE, **CANDIDATES, "reason": "暴雨红色预警", "report_ttl_minutes": 30, "idempotency_key": "service-storm-000003"},
        "scheduler",
    )
    with pytest.raises(PermissionDeniedError):
        service.confirm(plan["id"], domain="operations", actor="ops.lead", report_version=1, note="", permissions=frozenset({"reschedule.read"}))
    with pytest.raises(ValidationError):
        service.confirm(plan["id"], domain="security", actor="ops.lead", report_version=1, note="", permissions=frozenset({"*"}))
    with pytest.raises(ConflictError, match="未获得完整的双人确认"):
        service.apply(plan["id"], "scheduler")
