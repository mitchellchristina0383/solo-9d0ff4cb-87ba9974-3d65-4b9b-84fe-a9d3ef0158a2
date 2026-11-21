"""Rebuild-order drills: lost logical slot -> new slot -> receipt -> rebind."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import error_code


def open_order(client, sub="sub_reports", new_slot="slot_reports_v2", note=None):
    payload = {"subscription_name": sub, "new_slot_name": new_slot}
    if note is not None:
        payload["note"] = note
    return client.post("/api/rebuild-orders", json=payload)


def receipt(client, order, lsn=None, note=None):
    payload = {"snapshot_lsn": lsn or order["snapshot_lsn"]}
    if note is not None:
        payload["note"] = note
    return client.post(f"/api/rebuild-orders/{order['id']}/receipt", json=payload)


def abort(client, order):
    return client.post(f"/api/rebuild-orders/{order['id']}/abort", json={})


def slot_names(client):
    return [s["name"] for s in client.get("/api/inventory").json()["slots"]]


# ---------------------------------------------------------------------------
# Seed + open
# ---------------------------------------------------------------------------


def test_seed_has_lost_slot_with_stalled_subscription(demo):
    inventory = demo.get("/api/inventory").json()
    reports = next(s for s in inventory["slots"] if s["name"] == "slot_reports")
    assert reports["wal_status"] == "lost"
    assert reports["eligible"] is False
    sub = next(s for s in inventory["subscriptions"] if s["name"] == "sub_reports")
    assert sub["status"] == "stalled"
    assert sub["slot_name"] == "slot_reports"


def test_open_rebuild_order_records_snapshot_and_reserves_new_slot(demo):
    boundary = demo.get("/api/cluster").json()["wal_boundary_lsn"]
    created = open_order(demo, note="值班 DBA 开单")
    assert created.status_code == 201, created.text
    order = created.json()
    assert order["status"] == "open"
    assert order["snapshot_lsn"] == boundary
    assert order["source_slot_name"] == "slot_reports"
    assert order["new_slot_name"] == "slot_reports_v2"
    assert order["mismatch_count"] == 0

    # 新槽从快照位点起保留 WAL，库存计入其占用
    slot = demo.get("/api/slots/slot_reports_v2").json()
    assert slot["slot_type"] == "logical"
    assert slot["restart_lsn"] == boundary
    assert slot["confirmed_flush_lsn"] == boundary
    assert slot["occupancy"]["retained_bytes"] == 0
    assert slot["eligible"] is False
    assert slot["rebuild_order"]["role"] == "new_slot"
    assert slot["rebuild_order"]["order_id"] == order["id"]
    assert "reserved_by_rebuild_order" in {r["code"] for r in slot["blocking_reasons"]}

    # 原订阅维持停顿，保留原绑定和确认位点
    sub = demo.get("/api/subscriptions/sub_reports").json()
    assert sub["status"] == "stalled"
    assert sub["slot_name"] == "slot_reports"
    assert sub["confirmed_lsn"] == "0/1A00000"
    assert sub["open_rebuild_order_id"] == order["id"]

    # 原槽也标注了在办单
    source = demo.get("/api/slots/slot_reports").json()
    assert source["rebuild_order"]["role"] == "source_slot"


def test_open_requires_stalled_subscription_and_lost_logical_slot(demo):
    online = open_order(demo, sub="sub_orders", new_slot="slot_x1")
    assert online.status_code == 409
    assert error_code(online) == "subscription_not_stalled"

    # 停顿了但槽不是 lost：拒绝
    demo.post("/api/demo/subscriptions/sub_audit/stall", json={})
    not_lost = open_order(demo, sub="sub_audit", new_slot="slot_x2")
    assert not_lost.status_code == 409
    assert error_code(not_lost) == "slot_not_lost"

    missing = open_order(demo, sub="ghost", new_slot="slot_x3")
    assert missing.status_code == 404

    taken = open_order(demo, new_slot="slot_audit")
    assert taken.status_code == 409
    assert error_code(taken) == "slot_exists"

    # 开单全部失败：没有留下任何新槽或在办单
    assert demo.get("/api/rebuild-orders?status=open").json() == []
    assert "slot_x1" not in slot_names(demo)


def test_one_open_order_per_subscription(demo):
    first = open_order(demo)
    assert first.status_code == 201
    second = open_order(demo, new_slot="slot_reports_v3")
    assert second.status_code == 409
    assert error_code(second) == "rebuild_order_exists"
    # 中止后可再开
    assert abort(demo, first.json()).status_code == 200
    third = open_order(demo, new_slot="slot_reports_v3")
    assert third.status_code == 201


# ---------------------------------------------------------------------------
# WAL boundary movement while an order is open
# ---------------------------------------------------------------------------


def test_boundary_can_advance_but_not_cross_open_snapshot(demo):
    order = open_order(demo).json()
    snapshot = order["snapshot_lsn"]  # 0/2000000

    # 前推：允许，新槽占用随之增长并计入库存
    assert demo.put("/api/cluster", json={"wal_boundary_lsn": "0/2800000"}).status_code == 200
    slot = demo.get("/api/slots/slot_reports_v2").json()
    assert slot["occupancy"]["retained_bytes"] == 0x800000  # 8 MiB
    inventory = demo.get("/api/inventory").json()
    assert inventory["summary"]["slot_count"] == 7

    # 回退到快照位点：允许（不越过）
    assert demo.put("/api/cluster", json={"wal_boundary_lsn": snapshot}).status_code == 200

    # 回退越过快照位点：整次拒绝，边界保持不动
    below = demo.put("/api/cluster", json={"wal_boundary_lsn": "0/1F00000"})
    assert below.status_code == 409
    assert error_code(below) == "boundary_below_snapshot"
    detail = below.json()["error"]["detail"]
    assert detail["min_boundary_lsn"] == snapshot
    assert demo.get("/api/cluster").json()["wal_boundary_lsn"] == snapshot


def test_boundary_rollback_allowed_after_order_closes(demo):
    order = open_order(demo).json()
    assert abort(demo, order).status_code == 200
    # 在办单已结：回退不再受限
    assert demo.put("/api/cluster", json={"wal_boundary_lsn": "0/1000000"}).status_code == 200


# ---------------------------------------------------------------------------
# Guards while the order is open
# ---------------------------------------------------------------------------


def test_new_slot_is_protected_while_order_open(demo):
    open_order(demo)

    patch = demo.patch("/api/slots/slot_reports_v2", json={"note": "x"})
    assert patch.status_code == 409
    assert error_code(patch) == "slot_reserved_by_rebuild"

    delete = demo.delete("/api/slots/slot_reports_v2")
    assert delete.status_code == 409
    assert error_code(delete) == "slot_reserved_by_rebuild"

    bind = demo.post(
        "/api/subscriptions",
        json={"name": "sub_evil", "slot_name": "slot_reports_v2", "confirmed_lsn": "0/1"},
    )
    assert bind.status_code == 409
    assert error_code(bind) == "slot_reserved_by_rebuild"

    rebind = demo.patch("/api/subscriptions/sub_orders", json={"slot_name": "slot_reports_v2"})
    assert rebind.status_code == 409
    assert error_code(rebind) == "slot_reserved_by_rebuild"

    # 清理预览/确认同样动不了新槽
    preview = demo.post("/api/previews", json={"slot_names": ["slot_reports_v2"]}).json()
    confirmed = demo.post(
        f"/api/previews/{preview['id']}/confirm", json={"fingerprint": preview["fingerprint"]}
    )
    assert confirmed.status_code == 409
    assert error_code(confirmed) == "slots_not_eligible"
    assert demo.get("/api/slots/slot_reports_v2").status_code == 200


def test_original_subscription_is_locked_while_order_open(demo):
    open_order(demo)

    patch = demo.patch("/api/subscriptions/sub_reports", json={"confirmed_lsn": "0/1B00000"})
    assert patch.status_code == 409
    assert error_code(patch) == "subscription_rebuild_in_progress"

    resume = demo.post("/api/demo/subscriptions/sub_reports/resume")
    assert resume.status_code == 409
    assert error_code(resume) == "subscription_rebuild_in_progress"

    stall = demo.post("/api/demo/subscriptions/sub_reports/stall", json={})
    assert stall.status_code == 409

    sub = demo.get("/api/subscriptions/sub_reports").json()
    assert sub["status"] == "stalled"
    assert sub["slot_name"] == "slot_reports"
    assert sub["confirmed_lsn"] == "0/1A00000"


# ---------------------------------------------------------------------------
# Receipt: mismatch -> retry, match -> atomic rebind
# ---------------------------------------------------------------------------


def test_receipt_mismatch_keeps_order_open_and_allows_retry(demo):
    order = open_order(demo).json()
    bad = receipt(demo, order, lsn="0/1F00000")
    assert bad.status_code == 409
    assert error_code(bad) == "snapshot_mismatch"
    detail = bad.json()["error"]["detail"]
    assert detail["expected_snapshot_lsn"] == order["snapshot_lsn"]
    assert detail["reported_lsn"] == "0/1F00000"

    # 单仍在办，记录了一次不符；库存未变
    stored = demo.get(f"/api/rebuild-orders/{order['id']}").json()
    assert stored["status"] == "open"
    assert stored["mismatch_count"] == 1
    sub = demo.get("/api/subscriptions/sub_reports").json()
    assert sub["status"] == "stalled" and sub["slot_name"] == "slot_reports"

    # 重报正确位点：成功
    ok = receipt(demo, order)
    assert ok.status_code == 200
    assert ok.json()["order"]["status"] == "completed"


def test_receipt_match_rebinds_atomically_and_keeps_source_slot(demo):
    order = open_order(demo).json()
    response = receipt(demo, order, note="复制完成")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["already_closed"] is False

    closed = body["order"]
    assert closed["status"] == "completed"
    assert closed["receipt_lsn"] == order["snapshot_lsn"]
    assert closed["result"]["source_slot_retained"] == "slot_reports"

    # 订阅换绑到新槽并按快照位点恢复在线
    sub = demo.get("/api/subscriptions/sub_reports").json()
    assert sub["status"] == "online"
    assert sub["slot_name"] == "slot_reports_v2"
    assert sub["confirmed_lsn"] == order["snapshot_lsn"]
    assert sub["open_rebuild_order_id"] is None

    new_slot = demo.get("/api/slots/slot_reports_v2").json()
    assert new_slot["active"] is True
    assert new_slot["rebuild_order"] is None

    # 原槽留在库存，仍是 lost
    source = demo.get("/api/slots/slot_reports").json()
    assert source["wal_status"] == "lost"
    assert "slot_reports" in slot_names(demo)


def test_receipt_after_completion_is_idempotent_and_changes_nothing(demo):
    order = open_order(demo).json()
    assert receipt(demo, order).status_code == 200
    before = demo.get("/api/inventory").json()

    again = receipt(demo, order)
    assert again.status_code == 200
    assert again.json()["already_closed"] is True
    assert again.json()["order"]["status"] == "completed"

    # 结单后的回执不再改动库存
    after = demo.get("/api/inventory").json()
    assert after["revision"] == before["revision"]
    assert [s["name"] for s in after["slots"]] == [s["name"] for s in before["slots"]]
    # 已完成单也不能再中止
    assert abort(demo, order).status_code == 409


# ---------------------------------------------------------------------------
# Abort
# ---------------------------------------------------------------------------


def test_abort_releases_new_slot_and_keeps_subscription_stalled(demo):
    order = open_order(demo).json()
    response = abort(demo, order)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["already_aborted"] is False
    assert body["order"]["status"] == "aborted"
    assert body["order"]["result"]["released_slot"] == "slot_reports_v2"

    # 新槽释放，原订阅继续停顿、保留原绑定与确认位点
    assert demo.get("/api/slots/slot_reports_v2").status_code == 404
    sub = demo.get("/api/subscriptions/sub_reports").json()
    assert sub["status"] == "stalled"
    assert sub["slot_name"] == "slot_reports"
    assert sub["confirmed_lsn"] == "0/1A00000"

    # 幂等：重复中止不再改动
    again = abort(demo, order)
    assert again.status_code == 200
    assert again.json()["already_aborted"] is True


def test_receipt_after_abort_changes_nothing(demo):
    """中止后收到回执：409，库存保持中止后的样子。"""
    order = open_order(demo).json()
    assert abort(demo, order).status_code == 200
    before = demo.get("/api/inventory").json()

    late = receipt(demo, order)
    assert late.status_code == 409
    assert error_code(late) == "rebuild_order_not_open"

    after = demo.get("/api/inventory").json()
    assert after["revision"] == before["revision"]
    assert [s["name"] for s in after["slots"]] == [s["name"] for s in before["slots"]]
    assert "slot_reports_v2" not in slot_names(demo)
    sub = demo.get("/api/subscriptions/sub_reports").json()
    assert sub["status"] == "stalled" and sub["slot_name"] == "slot_reports"
    stored = demo.get(f"/api/rebuild-orders/{order['id']}").json()
    assert stored["status"] == "aborted"


# ---------------------------------------------------------------------------
# Listing / restart
# ---------------------------------------------------------------------------


def test_list_and_filter_orders(demo):
    order = open_order(demo).json()
    orders = demo.get("/api/rebuild-orders").json()
    assert [o["id"] for o in orders] == [order["id"]]
    assert demo.get("/api/rebuild-orders?status=completed").json() == []
    assert demo.get("/api/rebuild-orders?status=open").json()[0]["id"] == order["id"]

    assert demo.get("/api/rebuild-orders/rb-ghost").status_code == 404

    assert abort(demo, order).status_code == 200
    assert demo.get("/api/rebuild-orders?status=open").json() == []
    aborted = demo.get("/api/rebuild-orders?status=aborted").json()
    assert [o["id"] for o in aborted] == [order["id"]]


def test_open_order_survives_restart_and_can_resume(demo):
    """重启后在办单可查询、可续办（回执/中止照常）。"""
    order = open_order(demo).json()
    with TestClient(app) as restarted:  # 同一数据文件，模拟进程重启
        fetched = restarted.get(f"/api/rebuild-orders/{order['id']}")
        assert fetched.status_code == 200
        assert fetched.json()["status"] == "open"

        # 在办期间的守卫在重启后仍然有效
        assert restarted.delete("/api/slots/slot_reports_v2").status_code == 409
        below = restarted.put("/api/cluster", json={"wal_boundary_lsn": "0/1000000"})
        assert below.status_code == 409
        assert error_code(below) == "boundary_below_snapshot"

        # 续办：提交回执，原子换绑
        done = receipt(restarted, order)
        assert done.status_code == 200
        sub = restarted.get("/api/subscriptions/sub_reports").json()
        assert sub["status"] == "online"
        assert sub["slot_name"] == "slot_reports_v2"


def test_state_endpoint_lists_open_rebuild_orders(demo):
    order = open_order(demo).json()
    state = demo.get("/api/state").json()
    assert [o["id"] for o in state["rebuild_orders_open"]] == [order["id"]]
    event_types = [e["event_type"] for e in state["events"]]
    assert "rebuild_order_opened" in event_types
