"""启动校验、停供（503）与多进程状态协调测试。

场景背景：两批装箱扫描合在一起成环。旧版本服务未在启动时重放校验，
重启后仍报告健康，按盒查询还会给出旧路径。本文件覆盖：
* 启动时按全线序号重放旧 SQLite 卷中的生效扫描，冲突即停供；
* 停供时 GET /health 与 GET /boxes/{box} 返回 503 并指出首个冲突序号；
* POST /scans/batch 在停供期间仍可用，整批补传/更正重放无冲突后，
  扫描、归属与恢复供数一起生效；失败则保留原扫描和归属，继续停供；
* 同卷另一进程提交有效更正后，本进程不逐笔重放，凭已提交状态自动恢复；
* 查询始终以同一已提交状态为准，不逐笔重放扫描。
"""

import sqlite3
import threading

from app.db import connect, db_path, init_db
from app.main import app
from app.service import status
from fastapi.testclient import TestClient


def _scan(scan_no, seq, revision, child, parent):
    return {"scan_no": scan_no, "seq": seq, "revision": revision,
            "child": child, "parent": parent}


def _raw_insert(conn, scan_no, seq, revision, child, parent):
    """绕过业务逻辑直接写库，模拟旧版本服务留下的成环卷。"""

    conn.execute(
        "INSERT INTO scans(scan_no, seq, revision, child, parent) "
        "VALUES (?, ?, ?, ?, ?)",
        (scan_no, seq, revision, child, parent),
    )


# --------------------------------------------------------------------------- #
# 启动校验：健康 / 停供
# --------------------------------------------------------------------------- #
def test_startup_replays_valid_volume_and_serves(client):
    client.post("/scans/batch", json={"scans": [
        _scan("S1", 1, 1, "B:b1", "C:c1"),
        _scan("S2", 2, 1, "C:c1", "P:p1"),
    ]})
    # 重启：全新 TestClient 触发 lifespan 启动校验
    with TestClient(app) as c2:
        assert c2.get("/health").status_code == 200
        assert c2.get("/boxes/B:b1").json()["path"] == [
            "P:p1", "C:c1", "B:b1"]


def test_startup_conflict_marks_503_with_first_conflict_seq():
    # 直接向卷中写入两批合在一起成环的生效扫描（旧服务遗留）
    conn = connect()
    try:
        _raw_insert(conn, "E1", 1, 1, "C:c1", "C:c2")
        _raw_insert(conn, "E2", 2, 1, "C:c2", "C:c1")  # seq2 形成环
        conn.commit()
    finally:
        conn.close()

    with TestClient(app) as c:
        h = c.get("/health")
        assert h.status_code == 503
        body = h.json()
        assert body["error"] == "service_unavailable"
        assert body["conflict_seq"] == 2

        b = c.get("/boxes/B:b1")
        assert b.status_code == 503
        assert b.json()["conflict_seq"] == 2

        # 停供期间提交入口仍开放（文档可用）
        assert c.get("/docs").status_code == 200
        openapi = c.get("/openapi.json").json()
        assert "/scans/batch" in openapi["paths"]


def test_startup_role_violation_in_old_volume_also_stops_service():
    conn = connect()
    try:
        _raw_insert(conn, "R1", 7, 1, "B:b1", "P:p1")  # 盒直接上托盘
        conn.commit()
    finally:
        conn.close()

    with TestClient(app) as c:
        assert c.get("/health").status_code == 503
        assert c.get("/health").json()["conflict_seq"] == 7


# --------------------------------------------------------------------------- #
# 停供期间补传/更正：成功一起恢复，失败保留原状继续停供
# --------------------------------------------------------------------------- #
def test_correction_while_unavailable_restores_service_in_one_commit():
    conn = connect()
    try:
        _raw_insert(conn, "E1", 1, 1, "C:c1", "C:c2")
        _raw_insert(conn, "E2", 2, 1, "C:c2", "C:c1")  # 环
        _raw_insert(conn, "E3", 3, 1, "B:b1", "C:c1")
        conn.commit()
    finally:
        conn.close()

    with TestClient(app) as c:
        assert c.get("/health").status_code == 503

        # 更正 E2：让 c2 上托盘，环被解除
        r = c.post("/scans/batch", json={"scans": [
            _scan("E2", 2, 2, "C:c2", "P:p1"),
        ]})
        assert r.status_code == 200, r.text
        assert r.json()["accepted"] is True

        # 同一请求后扫描、归属、供数一起生效
        assert c.get("/health").status_code == 200
        p = c.get("/boxes/B:b1").json()
        assert p["path"] == ["P:p1", "C:c2", "C:c1", "B:b1"]

        with connect() as check:
            row = check.execute(
                "SELECT revision, parent FROM scans WHERE scan_no='E2'").fetchone()
            assert row["revision"] == 2 and row["parent"] == "P:p1"
            assert check.execute(
                "SELECT value FROM service_meta WHERE key='state_version'").fetchone() is not None


def test_failed_correction_keeps_old_scans_edges_and_stays_unavailable():
    conn = connect()
    try:
        _raw_insert(conn, "E1", 1, 1, "C:c1", "C:c2")
        _raw_insert(conn, "E2", 2, 1, "C:c2", "C:c1")
        conn.commit()
    finally:
        conn.close()

    with TestClient(app) as c:
        assert c.get("/health").status_code == 503

        # 低修订：直接 409，库中内容不变
        r = c.post("/scans/batch", json={"scans": [
            _scan("E2", 2, 1, "C:c2", "P:p1"),
        ]})
        assert r.status_code == 409

        # 高修订但重放仍冲突（把 c2 挂到 c1 下，环还在）——整批回滚
        r2 = c.post("/scans/batch", json={"scans": [
            _scan("E2", 2, 2, "C:c2", "C:c1"),
        ]})
        assert r2.status_code == 409
        assert r2.json()["conflict_seq"] == 2

        # 原扫描与归属保留，继续停供
        with connect() as check:
            row = check.execute(
                "SELECT revision FROM scans WHERE scan_no='E2'").fetchone()
            assert row["revision"] == 1
            n_edges = check.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        assert n_edges == 0  # 遗留坏卷未生成边表，失败更正也不得写入
        assert c.get("/health").status_code == 503
        assert c.get("/boxes/B:b1").status_code == 503


def test_batch_backfill_must_make_all_effective_scans_replay_clean():
    # 遗留：seq1 合法，seq2 角色违规
    conn = connect()
    try:
        _raw_insert(conn, "H1", 1, 1, "B:b1", "C:c1")
        _raw_insert(conn, "H2", 2, 1, "B:b2", "P:p1")
        conn.commit()
    finally:
        conn.close()

    with TestClient(app) as c:
        assert c.get("/health").json()["conflict_seq"] == 2

        # 只补一个新盒而不修复 seq2：全量重放仍在 seq2 冲突
        r = c.post("/scans/batch", json={"scans": [
            _scan("H3", 3, 1, "B:b3", "C:c1"),
        ]})
        assert r.status_code == 409 and r.json()["conflict_seq"] == 2
        assert c.get("/health").status_code == 503

        # 整批更正 seq2 后全量重放无冲突，随提交一起恢复供数
        r2 = c.post("/scans/batch", json={"scans": [
            _scan("H2", 2, 2, "B:b2", "C:c1"),
        ]})
        assert r2.status_code == 200
        assert c.get("/health").status_code == 200
        assert c.get("/boxes/B:b2").json()["path"] == ["C:c1", "B:b2"]


# --------------------------------------------------------------------------- #
# 多进程共享同一卷：以已提交状态为准，旧停供结论不能再次停供
# --------------------------------------------------------------------------- #
def test_other_process_fix_clears_stale_unavailable_without_replay_per_query():
    conn = connect()
    try:
        _raw_insert(conn, "E1", 1, 1, "C:c1", "C:c2")
        _raw_insert(conn, "E2", 2, 1, "C:c2", "C:c1")
        conn.commit()
    finally:
        conn.close()

    with TestClient(app) as c:
        assert c.get("/health").status_code == 503
        stale_version = status._version
        assert stale_version is not None

        # 模拟同卷另一进程：独立连接提交有效更正并推进版本号
        other = connect()
        try:
            other.execute(
                "INSERT INTO scans(scan_no, seq, revision, child, parent) "
                "VALUES ('E2', 2, 2, 'C:c2', 'P:p1') "
                "ON CONFLICT(scan_no) DO UPDATE SET seq=2, revision=2, "
                "child='C:c2', parent='P:p1', "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')"
            )
            other.execute("DELETE FROM edges")
            other.executemany(
                "INSERT INTO edges(child, parent) VALUES (?, ?)",
                [("C:c1", "C:c2"), ("C:c2", "P:p1")],
            )
            other.execute(
                "INSERT INTO service_meta(key, value) VALUES('state_version', 1) "
                "ON CONFLICT(key) DO UPDATE SET value = value + 1"
            )
            other.commit()
        finally:
            other.close()

        # 本进程未重启：下一次查询凭版本号感知已提交修复并恢复，
        # 较早的启动校验结果不会再次停供
        h = c.get("/health")
        assert h.status_code == 200
        assert status._version != stale_version
        assert c.get("/boxes/B:b1").status_code in (200, 404)  # 盒未在卷中，404 属正常
        # 再查一次：版本未变，直接用缓存，不重新重放
        assert c.get("/health").status_code == 200


def test_other_process_broken_commit_is_detected_on_next_query():
    # 健康运行中，另一进程提交了带冲突的原始数据（模拟绕过服务的脏写）
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200

        other = connect()
        try:
            _raw_insert(other, "X1", 10, 1, "C:a", "C:b")
            _raw_insert(other, "X2", 11, 1, "C:b", "C:a")  # 环
            other.execute(
                "INSERT INTO service_meta(key, value) VALUES('state_version', 1) "
                "ON CONFLICT(key) DO UPDATE SET value = value + 1"
            )
            other.commit()
        finally:
            other.close()

        h = c.get("/health")
        assert h.status_code == 503
        assert h.json()["conflict_seq"] == 11


def test_queries_serve_committed_state_no_per_scan_replay(client):
    # 正常提交后，多次查询结果一致且直接读已提交边表
    client.post("/scans/batch", json={"scans": [
        _scan("S1", 1, 1, "B:b1", "C:c1"),
        _scan("S2", 2, 1, "C:c1", "P:p1"),
    ]})
    first = client.get("/boxes/B:b1").json()
    for _ in range(3):
        assert client.get("/boxes/B:b1").json() == first
    assert client.get("/health").json() == {"status": "ok"}


def test_concurrent_queries_see_one_committed_state(client):
    client.post("/scans/batch", json={"scans": [
        _scan("S1", 1, 1, "B:b1", "C:c1"),
        _scan("S2", 2, 1, "C:c1", "P:p1"),
    ]})
    results = []
    errors = []

    def reader():
        c = TestClient(app)
        for _ in range(20):
            r = c.get("/boxes/B:b1")
            if r.status_code == 200:
                results.append(tuple(r.json()["path"]))
            else:
                errors.append(r.status_code)

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert set(results) == {("P:p1", "C:c1", "B:b1")}


# --------------------------------------------------------------------------- #
# 重启后按库中有效扫描确认服务状态
# --------------------------------------------------------------------------- #
def test_restart_reconfirms_health_after_recovery(tmp_path, monkeypatch):
    db_file = tmp_path / "restart-recover.db"
    monkeypatch.setenv("TRACE_DB_PATH", str(db_file))
    init_db(str(db_file))

    # 遗留坏卷
    raw = sqlite3.connect(str(db_file))
    raw.execute(
        "INSERT INTO scans(scan_no, seq, revision, child, parent) "
        "VALUES ('E1', 1, 1, 'C:c1', 'C:c2')"
    )
    raw.execute(
        "INSERT INTO scans(scan_no, seq, revision, child, parent) "
        "VALUES ('E2', 2, 1, 'C:c2', 'C:c1')"
    )
    raw.commit()
    raw.close()

    with TestClient(app) as c1:
        assert c1.get("/health").status_code == 503
        r = c1.post("/scans/batch", json={"scans": [
            _scan("E2", 2, 2, "C:c2", "P:p1"),
        ]})
        assert r.status_code == 200
        assert c1.get("/health").status_code == 200

    # 再次重启：按库中有效扫描确认，服务保持健康
    with TestClient(app) as c2:
        assert c2.get("/health").status_code == 200
        assert c2.get("/boxes/B:b1").status_code == 404


def test_legacy_volume_without_meta_version_is_bootstrapped(tmp_path, monkeypatch):
    db_file = tmp_path / "legacy.db"
    monkeypatch.setenv("TRACE_DB_PATH", str(db_file))
    init_db(str(db_file))

    # 模拟更旧版本卷：无 service_meta 记录，但扫描本身合法
    raw = sqlite3.connect(str(db_file))
    raw.execute(
        "INSERT INTO scans(scan_no, seq, revision, child, parent) "
        "VALUES ('S1', 1, 1, 'B:b1', 'C:c1')"
    )
    raw.execute(
        "INSERT INTO scans(scan_no, seq, revision, child, parent) "
        "VALUES ('S2', 2, 1, 'C:c1', 'P:p1')"
    )
    raw.commit()
    raw.close()

    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/boxes/B:b1").json()["path"] == [
            "P:p1", "C:c1", "B:b1"]
        with sqlite3.connect(str(db_file)) as check:
            row = check.execute(
                "SELECT value FROM service_meta WHERE key='state_version'").fetchone()
            assert row is not None and int(row[0]) >= 1
