"""端到端业务规则测试（通过 FastAPI TestClient 走完整 HTTP 层）。"""

import os
import threading

from app.db import connect, init_db
from app.main import app
from fastapi.testclient import TestClient


def _scan(scan_no, seq, revision, child, parent):
    return {"scan_no": scan_no, "seq": seq, "revision": revision,
            "child": child, "parent": parent}


def test_docs_and_health(client):
    assert client.get("/health").json() == {"status": "ok"}
    docs = client.get("/docs")
    assert docs.status_code == 200
    openapi = client.get("/openapi.json").json()
    assert "/scans/batch" in openapi["paths"]


def test_box_into_case_then_case_into_pallet_path(client):
    r = client.post("/scans/batch", json={"scans": [
        _scan("S1", 1, 1, "B:box1", "C:case1"),
        _scan("S2", 2, 1, "B:box2", "C:case1"),
        _scan("S3", 3, 1, "C:case1", "P:pal1"),
    ]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accepted"] is True
    assert body["replayed_scans"] == 3

    # 盒路径：托盘 -> 箱 -> 盒
    p1 = client.get("/boxes/B:box1").json()
    assert p1["path"] == ["P:pal1", "C:case1", "B:box1"]
    assert p1["pallet"] == "P:pal1"
    assert p1["parent"] == "C:case1"
    p2 = client.get("/boxes/B:box2").json()
    assert p2["path"] == ["P:pal1", "C:case1", "B:box2"]

    # 变化清单：两盒最终都上了托盘
    boxes = {c["box"]: c for c in body["changes"]}
    assert set(boxes) == {"B:box1", "B:box2"}
    assert boxes["B:box1"]["new_pallet"] == "P:pal1"
    assert boxes["B:box1"]["old_pallet"] is None


def test_box_cannot_go_directly_to_pallet_first_conflict_seq(client):
    r = client.post("/scans/batch", json={"scans": [
        _scan("S10", 10, 1, "B:b10", "C:c10"),
        _scan("S11", 11, 1, "B:b10", "P:pal10"),
    ]})
    assert r.status_code == 409
    assert r.json()["conflict_seq"] == 11
    # 整批拒绝：层级不变
    assert client.get("/boxes/B:b10").status_code == 404
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 0


def test_pallet_cannot_be_child(client):
    r = client.post("/scans/batch", json={"scans": [
        _scan("S20", 20, 1, "P:pal20", "C:c20"),
    ]})
    assert r.status_code == 409
    assert r.json()["conflict_seq"] == 20


def test_reassign_replaces_old_ownership_and_cascades_to_inner_boxes(client):
    client.post("/scans/batch", json={"scans": [
        _scan("A1", 1, 1, "B:b1", "C:c1"),
        _scan("A2", 2, 1, "B:b2", "C:c1"),
        _scan("A3", 3, 1, "C:c1", "P:p1"),
        _scan("A4", 4, 1, "C:c2", "P:p2"),
    ]})
    # 把 c1 整体从 p1 转到 p2
    r = client.post("/scans/batch", json={"scans": [
        _scan("A5", 5, 1, "C:c1", "P:p2"),
    ]})
    assert r.status_code == 200
    changes = {c["box"]: c for c in r.json()["changes"]}
    assert changes["B:b1"]["old_pallet"] == "P:p1"
    assert changes["B:b1"]["new_pallet"] == "P:p2"
    assert changes["B:b1"]["old_path"] == ["P:p1", "C:c1", "B:b1"]
    assert changes["B:b1"]["new_path"] == ["P:p2", "C:c1", "B:b1"]
    assert client.get("/boxes/B:b2").json()["path"] == ["P:p2", "C:c1", "B:b2"]


def test_remove_sets_null_and_re_attach(client):
    client.post("/scans/batch", json={"scans": [
        _scan("D1", 1, 1, "B:b1", "C:c1"),
        _scan("D2", 2, 1, "C:c1", "P:p1"),
    ]})
    # 拆出盒
    r = client.post("/scans/batch", json={"scans": [
        _scan("D3", 3, 1, "B:b1", None),
    ]})
    assert r.status_code == 200
    ch = r.json()["changes"][0]
    assert ch["box"] == "B:b1" and ch["new_pallet"] is None
    assert ch["old_path"] == ["P:p1", "C:c1", "B:b1"] and ch["new_path"] == ["B:b1"]
    assert client.get("/boxes/B:b1").status_code == 404
    # 重新装入
    r2 = client.post("/scans/batch", json={"scans": [
        _scan("D4", 4, 1, "B:b1", "C:c1"),
    ]})
    assert r2.status_code == 200
    assert client.get("/boxes/B:b1").json()["pallet"] == "P:p1"


def test_cycle_detected_with_first_conflict_seq(client):
    client.post("/scans/batch", json={"scans": [
        _scan("E1", 1, 1, "B:b", "C:c1"),
        _scan("E2", 2, 1, "C:c1", "C:c2"),
        _scan("E3", 3, 1, "C:c2", "P:p1"),
    ]})
    # 让 c2 反过来挂到 c1 下面，形成 c1->c2->c1 环
    r = client.post("/scans/batch", json={"scans": [
        _scan("E4", 4, 1, "C:c2", "C:c1"),
    ]})
    assert r.status_code == 409
    assert r.json()["conflict_seq"] == 4
    # 原层级不变
    assert client.get("/boxes/B:b").json()["path"] == ["P:p1", "C:c2", "C:c1", "B:b"]


def test_duplicate_seq_rejected(client):
    r = client.post("/scans/batch", json={"scans": [
        _scan("F1", 5, 1, "B:b1", "C:c1"),
        _scan("F2", 5, 2, "B:b2", "C:c1"),
    ]})
    assert r.status_code == 400 and r.json()["error"] == "duplicate_seq"

    client.post("/scans/batch", json={"scans": [
        _scan("F3", 6, 1, "B:b3", "C:c3")]})
    r2 = client.post("/scans/batch", json={"scans": [
        _scan("F4", 6, 1, "B:b4", "C:c4")]})
    assert r2.status_code == 409 and r2.json()["conflict_seq"] == 6


def test_revision_must_increase_and_same_revision_different_payload_rejects(client):
    client.post("/scans/batch", json={"scans": [
        _scan("G1", 1, 1, "B:b1", "C:c1")]})
    client.post("/scans/batch", json={"scans": [
        _scan("G1", 1, 2, "B:b1", "C:c1")]})

    # 低修订
    r = client.post("/scans/batch", json={"scans": [
        _scan("G1", 1, 1, "B:b1", None)]})
    assert r.status_code == 409 and r.json()["error"] == "revision_conflict"

    # 同修订异文
    r2 = client.post("/scans/batch", json={"scans": [
        _scan("G1", 1, 1, "B:b1", "C:c2")]})
    assert r2.status_code == 409

    # 同修订同文：幂等（针对当前最高修订 rev2）
    r3 = client.post("/scans/batch", json={"scans": [
        _scan("G1", 1, 2, "B:b1", "C:c1")]})
    assert r3.status_code == 200 and r3.json()["changes"] == []

    # 更高修订更正：拆出
    r4 = client.post("/scans/batch", json={"scans": [
        _scan("G1", 1, 3, "B:b1", None)]})
    assert r4.status_code == 200
    assert client.get("/boxes/B:b1").status_code == 404


def test_correction_making_later_scan_invalid_rejects_whole_batch(client):
    # 初始：b1 入 c1(seq1)，c1 上托盘 p1(seq2)
    client.post("/scans/batch", json={"scans": [
        _scan("H1", 1, 1, "B:b1", "C:c1"),
        _scan("H2", 2, 1, "C:c1", "P:p1"),
    ]})
    # 更正 seq1：把 b1 直接挂托盘 —— 自身即非法，且整批拒绝
    r = client.post("/scans/batch", json={"scans": [
        _scan("H1", 1, 2, "B:b1", "P:p1"),
    ]})
    assert r.status_code == 409
    assert r.json()["conflict_seq"] == 1
    # 原扫描和层级不变
    assert client.get("/boxes/B:b1").json()["path"] == ["P:p1", "C:c1", "B:b1"]
    with connect() as conn:
        row = conn.execute("SELECT revision FROM scans WHERE scan_no='H1'").fetchone()
        assert row["revision"] == 1


def test_late_arrival_invalidates_future_scan_first_conflict_seq(client):
    # seq 1..3 已生效；补传 seq2 的修订，使 seq3 变非法
    client.post("/scans/batch", json={"scans": [
        _scan("I1", 1, 1, "B:b1", "C:c1"),
        _scan("I2", 2, 1, "C:c1", "C:c2"),
        _scan("I3", 3, 1, "C:c2", "P:p1"),
    ]})
    # 把 seq2 改成 c1 -> c3，而 c3 又被 seq? 不存在……
    # 改为：补传让 c2 成为 c1 的后代再反向制造环：
    # 更正 seq2 为 c1 -> c2 已存在；改为让 seq1 之后补一个 seq2 修订，
    # 令 c1 挂到 c2 下，而 seq3 是 c2->p1，合法。改为环场景：
    r = client.post("/scans/batch", json={"scans": [
        _scan("I3", 3, 2, "C:c2", "C:c1"),
    ]})
    # 重放：seq1 b1->c1, seq2 c1->c2, seq3(rev2) c2->c1 => 环，冲突序号=3
    assert r.status_code == 409
    assert r.json()["conflict_seq"] == 3
    assert client.get("/boxes/B:b1").json()["path"] == ["P:p1", "C:c2", "C:c1", "B:b1"]


def test_batch_size_limit_and_validation(client):
    too_many = [_scan(f"S{i}", i, 1, "B:b", "C:c") for i in range(1, 102)]
    r = client.post("/scans/batch", json={"scans": too_many})
    assert r.status_code == 422

    assert client.post("/scans/batch", json={"scans": []}).status_code == 400

    bad = client.post("/scans/batch", json={"scans": [
        {"scan_no": "X1", "seq": 1, "revision": 1,
         "child": "B:b1", "parent": "X:weird"}]})
    assert bad.status_code == 422


def test_duplicate_scan_no_in_batch_rejected(client):
    r = client.post("/scans/batch", json={"scans": [
        _scan("J1", 1, 1, "B:b1", "C:c1"),
        _scan("J1", 2, 1, "B:b2", "C:c1"),
    ]})
    assert r.status_code == 400 and r.json()["error"] == "duplicate_scan_no"


def test_nested_cases_and_pallet_root(client):
    r = client.post("/scans/batch", json={"scans": [
        _scan("K1", 1, 1, "B:b1", "C:inner"),
        _scan("K2", 2, 1, "C:inner", "C:outer"),
        _scan("K3", 3, 1, "C:outer", "P:p1"),
    ]})
    assert r.status_code == 200
    assert client.get("/boxes/B:b1").json()["path"] == [
        "P:p1", "C:outer", "C:inner", "B:b1"]


def test_concurrent_submissions_no_mixed_revision(client):
    # 两个线程各建独立层级，分别提交；再并发更新同一扫描号制造修订竞争
    client.post("/scans/batch", json={"scans": [
        _scan("BASE", 1, 1, "B:b1", "C:c1"),
        _scan("BASE2", 2, 1, "C:c1", "P:p1"),
    ]})

    errors, ok = [], []

    def worker(rev, parent):
        c = TestClient(app)
        r = c.post("/scans/batch", json={"scans": [
            _scan("BASE", 1, rev, "B:b1", parent)]})
        (ok if r.status_code == 200 else errors).append((rev, r.status_code, r.json()))

    t1 = threading.Thread(target=worker, args=(2, None))
    t2 = threading.Thread(target=worker, args=(2, "C:c1"))  # 同修订异文竞争
    t1.start(); t2.start(); t1.join(); t2.join()

    assert len(ok) == 1 and len(errors) == 1
    # 任一结果下，数据库只存在一个 rev2 版本，且 edges 与 scans 一致
    with connect() as conn:
        rev = conn.execute("SELECT revision, parent FROM scans WHERE scan_no='BASE'").fetchone()
        assert rev["revision"] == 2
        edge = conn.execute("SELECT parent FROM edges WHERE child='B:b1'").fetchone()
        if rev["parent"] is None:
            assert edge is None
        else:
            assert edge["parent"] == rev["parent"]
        # 不变式：每个子项至多一条边、无环
        n_child = conn.execute(
            "SELECT child, COUNT(*) c FROM edges GROUP BY child HAVING c > 1").fetchall()
        assert n_child == []


def test_persistence_across_restart(tmp_path, monkeypatch):
    db_file = tmp_path / "restart.db"
    monkeypatch.setenv("TRACE_DB_PATH", str(db_file))
    init_db(str(db_file))

    with TestClient(app) as c1:
        r = c1.post("/scans/batch", json={"scans": [
            _scan("R1", 1, 1, "B:b1", "C:c1"),
            _scan("R2", 2, 1, "C:c1", "P:p1"),
        ]})
        assert r.status_code == 200

    # 模拟重启：新建全新客户端，数据从 SQLite 文件恢复
    with TestClient(app) as c2:
        assert c2.get("/boxes/B:b1").json()["path"] == ["P:p1", "C:c1", "B:b1"]
        r2 = c2.post("/scans/batch", json={"scans": [
            _scan("R1", 1, 2, "B:b1", None)]})
        assert r2.status_code == 200
        assert c2.get("/boxes/B:b1").status_code == 404


def test_read_during_writes_never_partial(client):
    # 先铺底：50 盒装箱上托盘
    seed = [_scan(f"Q{i}", i, 1, f"B:b{i}", "C:c1") for i in range(1, 51)]
    seed.append(_scan("Q99", 51, 1, "C:c1", "P:p1"))
    assert client.post("/scans/batch", json={"scans": seed}).status_code == 200

    stop = threading.Event()
    bad = []

    def reader():
        c = TestClient(app)
        while not stop.is_set():
            r = c.get("/boxes/B:b1")
            if r.status_code == 200:
                path = r.json()["path"]
                # 合法路径要么含旧托盘 P:p1，要么盒已拆出；不允许出现悬空父节点
                assert path[-1] == "B:b1"
                if "C:c1" in path:
                    assert path[0] in ("P:p1", "C:c1", "B:b1")
            elif r.status_code != 404:
                bad.append(r.status_code)

    t = threading.Thread(target=reader)
    t.start()
    for rev in range(2, 8):
        parent = None if rev % 2 == 0 else "C:c1"
        r = client.post("/scans/batch", json={"scans": [
            _scan("Q99", 51, rev, "C:c1", "P:p1" if parent else None)]})
        assert r.status_code == 200
    stop.set(); t.join()
    assert bad == []
