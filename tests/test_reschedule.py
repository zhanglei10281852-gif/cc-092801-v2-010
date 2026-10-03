from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from app.core.clock import FrozenClock
from app.core.errors import ConflictError
from app.core.security import Principal
from app.database import get_connection, init_db
from app.reschedule.service import RescheduleService

SOURCE_DATE = "2026-10-05"
REHEARSAL_PAYLOAD = {
    "source_date": SOURCE_DATE,
    "window_start": "2026-10-06",
    "window_end": "2026-10-08",
    "reason": "暴雨红色预警，整体改期",
}


def make_user(client, admin, username: str, role_code: str, permission_codes: list[str], display_name: str) -> dict:
    role = client.post("/api/roles", headers=admin["headers"], json={"code": role_code, "name": role_code, "permission_codes": permission_codes})
    assert role.status_code == 201, role.text
    user = client.post("/api/users", headers=admin["headers"], json={"username": username, "password": "Passw0rd!x", "display_name": display_name, "role_codes": [role_code]})
    assert user.status_code == 201, user.text
    login = client.post("/api/auth/login", json={"username": username, "password": "Passw0rd!x", "client_label": "tests"})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['token']}"}


@pytest.fixture()
def ops_headers(client, admin) -> dict:
    return make_user(client, admin, "ops.lead", "reschedule.ops", ["reschedule.read", "reschedule.rehearse", "reschedule.confirm.operations"], "运营主管")


@pytest.fixture()
def finance_headers(client, admin) -> dict:
    return make_user(client, admin, "finance.lead", "reschedule.finance", ["reschedule.read", "reschedule.confirm.finance"], "财务主管")


def seed_orders(client, admin) -> None:
    orders = [
        {"order_code": "WED-1001", "ceremony_type": "wedding", "ceremony_date": SOURCE_DATE, "venue": "海棠厅", "customer_name": "张先生", "base_fee_cents": 880000,
         "resources": [
             {"resource_type": "staff", "resource_name": "主持人甲", "commitment_fee_cents": 50000},
             {"resource_type": "vehicle", "resource_name": "车队A", "commitment_fee_cents": 80000},
             {"resource_type": "vendor", "resource_name": "花艺供应商", "commitment_fee_cents": 120000},
         ]},
        {"order_code": "WED-1002", "ceremony_type": "wedding", "ceremony_date": SOURCE_DATE, "venue": "玫瑰厅", "customer_name": "李女士", "base_fee_cents": 660000,
         "resources": [
             {"resource_type": "staff", "resource_name": "主持人乙", "commitment_fee_cents": 50000},
             {"resource_type": "vehicle", "resource_name": "车队B", "commitment_fee_cents": 80000},
             {"resource_type": "vendor", "resource_name": "灯光供应商", "commitment_fee_cents": 60000},
         ]},
        {"order_code": "MEM-2001", "ceremony_type": "memorial", "ceremony_date": SOURCE_DATE, "venue": "静安厅", "customer_name": "陈先生", "base_fee_cents": 320000,
         "resources": [
             {"resource_type": "staff", "resource_name": "司仪丙", "commitment_fee_cents": 30000},
             {"resource_type": "vehicle", "resource_name": "车队C", "commitment_fee_cents": 70000},
             {"resource_type": "vendor", "resource_name": "礼仪供应商", "commitment_fee_cents": 40000},
         ]},
        # 候选日 2026-10-06 上已占用 车队A 的无关订单
        {"order_code": "OTHER-9001", "ceremony_type": "wedding", "ceremony_date": "2026-10-06", "venue": "湖畔厅", "customer_name": "刘先生", "base_fee_cents": 500000,
         "resources": [{"resource_type": "vehicle", "resource_name": "车队A", "commitment_fee_cents": 80000}]},
    ]
    for order in orders:
        response = client.post("/api/reschedule/orders", headers=admin["headers"], json=order)
        assert response.status_code == 201, response.text


def create_rehearsal(client, headers) -> dict:
    response = client.post("/api/reschedule/rehearsals", headers=headers, json=REHEARSAL_PAYLOAD)
    assert response.status_code == 201, response.text
    return response.json()


def confirm(client, headers, rehearsal_id: int, payload: dict):
    return client.post(f"/api/reschedule/rehearsals/{rehearsal_id}/confirmations", headers=headers, json=payload)


def test_rehearsal_report_and_dual_confirmation_apply(client, admin, ops_headers, finance_headers):
    seed_orders(client, admin)
    rehearsal = create_rehearsal(client, ops_headers)
    rehearsal_id = rehearsal["id"]
    assert rehearsal["status"] == "awaiting_confirmation"
    assert rehearsal["target_date"] == "2026-10-07"
    assert rehearsal["progress"]["confirmations_received"] == []

    # 预演与报告不改变当前生效数据
    orders = client.get(f"/api/reschedule/orders?ceremony_date={SOURCE_DATE}", headers=admin["headers"]).json()["items"]
    assert len(orders) == 3
    assert all(order["status"] == "scheduled" for order in orders)

    # 报告摘要：受影响订单、候选日冲突、可迁移资源与预计费用差异
    report = client.get(f"/api/reschedule/rehearsals/{rehearsal_id}/report", headers=ops_headers).json()
    assert report["version"] == 1
    assert report["summary"]["affected_order_count"] == 3
    assert report["summary"]["recommended_target_date"] == "2026-10-07"
    assert report["summary"]["total_fee_delta_cents"] == 44000 + 33000 + 16000
    by_date = {item["date"]: item for item in report["summary"]["candidates"]}
    assert by_date["2026-10-06"]["feasible"] is False
    assert by_date["2026-10-06"]["unmigratable_resource_count"] == 1
    assert by_date["2026-10-07"]["feasible"] is True
    evaluation_6 = next(item for item in report["details"]["evaluations"] if item["date"] == "2026-10-06")
    assert evaluation_6["conflicts"] == [{"order_code": "WED-1001", "resource_type": "vehicle", "resource_name": "车队A"}]
    assert evaluation_6["fee_delta_cents"] == 93000 + 16000  # 93000 服务费 + 车队A 违约金
    assert client.get(f"/api/reschedule/rehearsals/{rehearsal_id}/result", headers=ops_headers).status_code == 409

    # 第一名确认人（运营权限组）
    first = confirm(client, ops_headers, rehearsal_id, {"group": "operations", "note": "暴雨红色预警，同意改期"})
    assert first.status_code == 201, first.text
    assert first.json()["applied"] is False
    progress = client.get(f"/api/reschedule/rehearsals/{rehearsal_id}", headers=ops_headers).json()
    assert progress["status"] == "awaiting_confirmation"
    assert progress["progress"]["confirmations_received"] == ["operations"]

    # 第二名确认人（财务权限组）确认同一份报告后原子切换
    second = confirm(client, finance_headers, rehearsal_id, {"group": "finance", "report_version": 1})
    assert second.status_code == 201, second.text
    assert second.json()["applied"] is True
    assert second.json()["rehearsal"]["status"] == "applied"

    # 生效数据整体切换：日期、状态与累计改期费用
    moved = client.get("/api/reschedule/orders?ceremony_date=2026-10-07", headers=admin["headers"]).json()["items"]
    assert sorted(order["order_code"] for order in moved) == ["MEM-2001", "WED-1001", "WED-1002"]
    assert all(order["status"] == "rescheduled" for order in moved)
    fees = {order["order_code"]: order["reschedule_fee_cents"] for order in moved}
    assert fees == {"WED-1001": 44000, "WED-1002": 33000, "MEM-2001": 16000}
    assert client.get(f"/api/reschedule/orders?ceremony_date={SOURCE_DATE}", headers=admin["headers"]).json()["items"] == []

    # 确认记录、最终版本与事件流
    confirmations = client.get(f"/api/reschedule/rehearsals/{rehearsal_id}/confirmations", headers=finance_headers).json()["items"]
    assert [(item["permission_group"], item["actor"]) for item in confirmations] == [("operations", "ops.lead"), ("finance", "finance.lead")]
    result = client.get(f"/api/reschedule/rehearsals/{rehearsal_id}/result", headers=finance_headers).json()
    assert result["target_date"] == "2026-10-07"
    assert result["total_fee_delta_cents"] == 93000
    assert len(result["moves"]) == 3
    wed = next(move for move in result["moves"] if move["order_code"] == "WED-1001")
    assert wed["from_date"] == SOURCE_DATE and wed["to_date"] == "2026-10-07"
    assert {item["resource_name"] for item in wed["moved_resources"]} == {"海棠厅", "主持人甲", "车队A", "花艺供应商"}
    actions = [item["action"] for item in client.get(f"/api/reschedule/rehearsals/{rehearsal_id}/events", headers=admin["headers"]).json()["items"]]
    assert actions == ["created", "analyzed", "confirmed", "confirmed", "applied"]
    listing = client.get("/api/reschedule/rehearsals?status=applied", headers=ops_headers).json()["items"]
    assert [item["id"] for item in listing] == [rehearsal_id]


def test_duplicate_confirmation_fails_safely(client, admin, ops_headers, finance_headers):
    seed_orders(client, admin)
    rehearsal_id = create_rehearsal(client, ops_headers)["id"]
    assert confirm(client, ops_headers, rehearsal_id, {"group": "operations"}).status_code == 201

    # 同一人员重复确认
    again = confirm(client, ops_headers, rehearsal_id, {"group": "operations"})
    assert again.status_code == 409
    assert "重复确认" in again.json()["error"]["message"]
    # 同一权限组的另一人重复确认
    deputy = make_user(client, admin, "ops.deputy", "reschedule.ops2", ["reschedule.read", "reschedule.confirm.operations"], "运营副手")
    same_group = confirm(client, deputy, rehearsal_id, {"group": "operations"})
    assert same_group.status_code == 409
    assert "权限组" in same_group.json()["error"]["message"]
    # 报告版本不匹配
    mismatch = confirm(client, finance_headers, rehearsal_id, {"group": "finance", "report_version": 99})
    assert mismatch.status_code == 409

    # 安全失败不破坏预演，财务确认后仍可正常切换
    final = confirm(client, finance_headers, rehearsal_id, {"group": "finance"})
    assert final.status_code == 201 and final.json()["applied"] is True
    events = client.get(f"/api/reschedule/rehearsals/{rehearsal_id}/events", headers=ops_headers).json()["items"]
    assert [item["action"] for item in events].count("confirmation_rejected") == 3


def test_rehearsal_blocked_when_all_candidates_conflict(client, admin, ops_headers, finance_headers):
    seed_orders(client, admin)
    for index, day in enumerate(["2026-10-06", "2026-10-07", "2026-10-08"]):
        response = client.post("/api/reschedule/orders", headers=admin["headers"], json={
            "order_code": f"OTHER-910{index}", "ceremony_type": "wedding", "ceremony_date": day,
            "venue": f"备用厅{index}", "customer_name": "王先生", "base_fee_cents": 100000,
            "resources": [{"resource_type": "staff", "resource_name": "主持人甲", "commitment_fee_cents": 50000}],
        })
        assert response.status_code == 201, response.text
    rehearsal = create_rehearsal(client, ops_headers)
    assert rehearsal["status"] == "blocked"
    assert "资源冲突" in rehearsal["failure_reason"]

    # 报告仍可供复核，但没有推荐日期
    report = client.get(f"/api/reschedule/rehearsals/{rehearsal['id']}/report", headers=ops_headers).json()
    assert report["summary"]["recommended_target_date"] is None
    assert report["summary"]["feasible_candidate_count"] == 0
    # 被阻塞的预演不能接受确认
    assert confirm(client, ops_headers, rehearsal["id"], {"group": "operations"}).status_code == 409
    assert confirm(client, finance_headers, rehearsal["id"], {"group": "finance"}).status_code == 409
    # 订单保持原状
    orders = client.get(f"/api/reschedule/orders?ceremony_date={SOURCE_DATE}", headers=admin["headers"]).json()["items"]
    assert len(orders) == 3 and all(order["status"] == "scheduled" for order in orders)


def test_apply_fails_safely_when_resource_becomes_unavailable(client, admin, ops_headers, finance_headers):
    seed_orders(client, admin)
    rehearsal_id = create_rehearsal(client, ops_headers)["id"]
    assert confirm(client, ops_headers, rehearsal_id, {"group": "operations"}).status_code == 201
    # 报告生成后，车队A 在推荐日期被其他订单占用
    late = client.post("/api/reschedule/orders", headers=admin["headers"], json={
        "order_code": "OTHER-9200", "ceremony_type": "wedding", "ceremony_date": "2026-10-07",
        "venue": "山顶厅", "customer_name": "赵女士", "base_fee_cents": 200000,
        "resources": [{"resource_type": "vehicle", "resource_name": "车队A", "commitment_fee_cents": 80000}],
    })
    assert late.status_code == 201, late.text

    second = confirm(client, finance_headers, rehearsal_id, {"group": "finance"})
    assert second.status_code == 409
    assert "部分资源在确认后不可用" in second.json()["error"]["message"]
    rehearsal = client.get(f"/api/reschedule/rehearsals/{rehearsal_id}", headers=ops_headers).json()
    assert rehearsal["status"] == "failed"
    assert "车队A" in rehearsal["failure_reason"]
    # 原子切换未发生：订单保持原日期，确认记录保留，无最终版本
    orders = client.get(f"/api/reschedule/orders?ceremony_date={SOURCE_DATE}", headers=admin["headers"]).json()["items"]
    assert len(orders) == 3 and all(order["status"] == "scheduled" for order in orders)
    confirmations = client.get(f"/api/reschedule/rehearsals/{rehearsal_id}/confirmations", headers=ops_headers).json()["items"]
    assert len(confirmations) == 2
    assert client.get(f"/api/reschedule/rehearsals/{rehearsal_id}/result", headers=ops_headers).status_code == 409
    actions = [item["action"] for item in client.get(f"/api/reschedule/rehearsals/{rehearsal_id}/events", headers=ops_headers).json()["items"]]
    assert actions[-1] == "apply_failed"


def test_report_expiry_safe_failure(client):
    init_db()
    clock = FrozenClock(datetime(2026, 9, 30, 8, 0, tzinfo=UTC))
    service = RescheduleService(get_connection(), clock)
    ops = Principal(user_id=1, username="ops", display_name="运营主管", department_id=None, permissions=frozenset({"reschedule.rehearse", "reschedule.confirm.operations"}), session_id=1)
    finance = Principal(user_id=2, username="finance", display_name="财务主管", department_id=None, permissions=frozenset({"reschedule.confirm.finance"}), session_id=2)
    service.create_order({
        "order_code": "WED-1001", "ceremony_type": "wedding", "ceremony_date": date(2026, 10, 5),
        "venue": "海棠厅", "customer_name": "张先生", "base_fee_cents": 880000,
        "resources": [{"resource_type": "staff", "resource_name": "主持人甲", "commitment_fee_cents": 50000}],
    }, ops)
    rehearsal = service.create_rehearsal({
        "source_date": date(2026, 10, 5), "window_start": date(2026, 10, 6),
        "window_end": date(2026, 10, 7), "reason": "暴雨红色预警",
    }, ops)
    assert rehearsal["status"] == "awaiting_confirmation"

    clock.advance(seconds=1801)
    with pytest.raises(ConflictError, match="过期"):
        service.confirm(rehearsal["id"], ops, "operations", None, "")
    expired = service.get_rehearsal(rehearsal["id"])
    assert expired["status"] == "expired"
    assert "过期" in expired["failure_reason"]
    with pytest.raises(ConflictError):
        service.confirm(rehearsal["id"], finance, "finance", None, "")
    order = service.list_orders(date="2026-10-05")[0]
    assert order["status"] == "scheduled" and order["reschedule_fee_cents"] == 0


def test_restart_recovery_marks_incomplete_rehearsals(client, admin, ops_headers):
    seed_orders(client, admin)
    analyzing_id = create_rehearsal(client, ops_headers)["id"]
    expired_id = create_rehearsal(client, ops_headers)["id"]
    connection = get_connection()
    # 模拟服务崩溃：一个预演停在分析中，另一个报告已过确认时限
    connection.execute("UPDATE reschedule_rehearsals SET status='analyzing' WHERE id=?", (analyzing_id,))
    connection.execute("UPDATE reschedule_rehearsals SET expires_at='2020-01-01T00:00:00+00:00' WHERE id=?", (expired_id,))

    from app.main import app

    with TestClient(app) as restarted:  # 重启触发启动恢复
        first = restarted.get(f"/api/reschedule/rehearsals/{analyzing_id}", headers=ops_headers).json()
        second = restarted.get(f"/api/reschedule/rehearsals/{expired_id}", headers=ops_headers).json()
        assert first["status"] == "failed" and "重启" in first["failure_reason"]
        assert second["status"] == "expired" and "过期" in second["failure_reason"]
        # 未完成的预演不会被当作已执行：订单保持原日期
        orders = restarted.get(f"/api/reschedule/orders?ceremony_date={SOURCE_DATE}", headers=admin["headers"]).json()["items"]
        assert len(orders) == 3 and all(order["status"] == "scheduled" for order in orders)


def test_permissions_enforced(client, admin, ops_headers):
    seed_orders(client, admin)
    reader = make_user(client, admin, "reader", "reschedule.reader", ["reschedule.read"], "只读人员")
    assert client.post("/api/reschedule/rehearsals", headers=reader, json=REHEARSAL_PAYLOAD).status_code == 403
    rehearsal_id = create_rehearsal(client, ops_headers)["id"]
    # 只读人员可查询进度、报告与确认记录
    assert client.get(f"/api/reschedule/rehearsals/{rehearsal_id}", headers=reader).status_code == 200
    assert client.get(f"/api/reschedule/rehearsals/{rehearsal_id}/report", headers=reader).status_code == 200
    assert client.get(f"/api/reschedule/rehearsals/{rehearsal_id}/confirmations", headers=reader).status_code == 200
    # 运营权限不能冒充财务权限组确认
    assert confirm(client, ops_headers, rehearsal_id, {"group": "finance"}).status_code == 403
    # 未认证请求被拒绝
    assert client.get(f"/api/reschedule/rehearsals/{rehearsal_id}").status_code == 401


def test_rehearsal_without_orders_fails_with_reason(client, admin, ops_headers):
    rehearsal = create_rehearsal(client, ops_headers)
    assert rehearsal["status"] == "failed"
    assert "没有可改期" in rehearsal["failure_reason"]
    assert client.get(f"/api/reschedule/rehearsals/{rehearsal['id']}/report", headers=ops_headers).status_code == 404


def test_validation_of_window_and_orders(client, admin, ops_headers):
    contains_source = client.post("/api/reschedule/rehearsals", headers=ops_headers, json={
        "source_date": "2026-10-05", "window_start": "2026-10-04", "window_end": "2026-10-06", "reason": "暴雨预警",
    })
    assert contains_source.status_code == 422
    too_long = client.post("/api/reschedule/rehearsals", headers=ops_headers, json={
        "source_date": "2026-10-05", "window_start": "2026-10-06", "window_end": "2026-10-30", "reason": "暴雨预警",
    })
    assert too_long.status_code == 422
    order = {"order_code": "WED-1001", "ceremony_type": "wedding", "ceremony_date": SOURCE_DATE, "venue": "海棠厅", "customer_name": "张先生", "base_fee_cents": 1000, "resources": []}
    assert client.post("/api/reschedule/orders", headers=admin["headers"], json=order).status_code == 201
    assert client.post("/api/reschedule/orders", headers=admin["headers"], json=order).status_code == 409
