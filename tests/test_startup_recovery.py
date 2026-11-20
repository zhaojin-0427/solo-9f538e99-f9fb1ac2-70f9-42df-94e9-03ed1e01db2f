"""启动校验、停供与恢复供数的端到端测试。

覆盖：
* 旧 SQLite 卷中的生效扫描在启动时按全线序号重放，冲突则停供；
* ``GET /health`` 与 ``GET /boxes/{box}`` 均 503，指出首个冲突序号；
* 停供期间仍可整批补传/更正，成功则扫描、归属与恢复供数同一事务生效；
* 提交失败保留原扫描与归属并继续停供；
* 多进程共享同一卷：查询以已提交状态为准，旧启动校验结果不会覆盖有效更正。
"""

import os
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.main import app


def _scan(scan_no, seq, revision, child, parent):
    return {"scan_no": scan_no, "seq": seq, "revision": revision,
            "child": child, "parent": parent}


def _seed_raw(db_file: Path, rows, edges=None) -> None:
    """模拟“旧 SQLite 卷”：直接写入扫描（绕过校验），模拟历史脏数据。"""

    init_db(str(db_file))
    conn = sqlite3.connect(str(db_file), isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(
            "INSERT INTO scans(scan_no, seq, revision, child, parent) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        if edges:
            conn.executemany("INSERT INTO edges(child, parent) VALUES (?, ?)", edges)
        conn.execute("COMMIT")
    finally:
        conn.close()


@pytest.fixture()
def db_file(tmp_path, monkeypatch):
    path = tmp_path / "startup.db"
    monkeypatch.setenv("TRACE_DB_PATH", str(path))
    return path


def test_restart_with_valid_scans_serves_and_repairs_edges(db_file):
    _seed_raw(
        db_file,
        [
            ("S1", 1, 1, "B:b1", "C:c1"),
            ("S2", 2, 1, "C:c1", "P:p1"),
        ],
        edges=[("B:b1", "C:c1")],  # 旧卷边表缺了 C:c1 -> P:p1
    )

    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
        body = c.get("/health").json()
        assert body == {"status": "ok"}
        assert c.get("/boxes/B:b1").json()["path"] == ["P:p1", "C:c1", "B:b1"]

    # 启动校验把修复后的归属落库
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 2
        state = conn.execute("SELECT status FROM service_state WHERE id=1").fetchone()
        assert state["status"] == "ok"


def test_restart_conflict_suspends_health_and_box_and_reports_first_seq(db_file):
    _seed_raw(
        db_file,
        [
            ("S1", 1, 1, "B:b1", "C:c1"),
            ("S2", 2, 1, "C:c1", "C:c2"),
            ("S3", 3, 1, "C:c2", "C:c1"),  # 环：首个冲突序号 3
        ],
        edges=[("B:b1", "C:c1")],
    )

    with TestClient(app) as c:
        health = c.get("/health")
        assert health.status_code == 503
        assert health.json()["status"] == "conflict"
        assert health.json()["conflict_seq"] == 3

        box = c.get("/boxes/B:b1")
        assert box.status_code == 503
        assert box.json()["conflict_seq"] == 3
        assert "path" not in box.json()  # 不返回旧路径

        # 停供不影响文档与提交入口
        assert c.get("/docs").status_code == 200
        assert c.get("/openapi.json").status_code == 200

        # 失败提交：保留原扫描与归属，继续停供
        bad = c.post("/scans/batch", json={"scans": [
            _scan("S3", 3, 2, "C:c2", "B:b1"),  # 角色违规
        ]})
        assert bad.status_code == 409 and bad.json()["conflict_seq"] == 3
        assert c.get("/health").status_code == 503
        with connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == 3
            assert conn.execute("SELECT revision FROM scans WHERE scan_no='S3'").fetchone()[0] == 1
            # 停供不改写旧归属：旧卷中已有的边保持原样
            assert conn.execute(
                "SELECT parent FROM edges WHERE child='B:b1'").fetchone()[0] == "C:c1"


def test_batch_correction_during_suspension_restores_serving_atomically(db_file):
    _seed_raw(
        db_file,
        [
            ("S1", 1, 1, "B:b1", "C:c1"),
            ("S2", 2, 1, "C:c1", "C:c2"),
            ("S3", 3, 1, "C:c2", "C:c1"),
        ],
    )

    with TestClient(app) as c:
        assert c.get("/health").status_code == 503
        assert c.get("/boxes/B:b1").status_code == 503

        # 更正：把 seq3 拆出，整批重放无冲突
        fixed = c.post("/scans/batch", json={"scans": [
            _scan("S3", 3, 2, "C:c2", None),
        ]})
        assert fixed.status_code == 200, fixed.text
        assert fixed.json()["accepted"] is True

        assert c.get("/health").json() == {"status": "ok"}
        assert c.get("/boxes/B:b1").json()["path"] == ["C:c2", "C:c1", "B:b1"]

    with connect() as conn:
        assert conn.execute("SELECT revision FROM scans WHERE scan_no='S3'").fetchone()[0] == 2
        assert conn.execute("SELECT status FROM service_state WHERE id=1").fetchone()[0] == "ok"


def test_correction_must_fix_every_conflict_before_serving_restored(db_file):
    _seed_raw(
        db_file,
        [
            ("S1", 1, 1, "B:b1", "C:c1"),
            ("S2", 2, 1, "P:bad", "C:c2"),  # 托盘作子项：首个冲突 2
            ("S3", 3, 1, "C:c1", "C:c2"),  # seq2 修正后与 c2 -> c1 成环
        ],
    )

    with TestClient(app) as c:
        health = c.get("/health")
        assert health.status_code == 503 and health.json()["conflict_seq"] == 2

        # 只修正 seq2 后，seq3 的 c1 -> c2 与 c2 -> c1 成环，仍停供
        partial = c.post("/scans/batch", json={"scans": [
            _scan("S2", 2, 2, "C:c2", "C:c1"),
        ]})
        assert partial.status_code == 409 and partial.json()["conflict_seq"] == 3
        assert c.get("/health").status_code == 503
        with connect() as conn:
            assert conn.execute(
                "SELECT revision FROM scans WHERE scan_no='S2'").fetchone()[0] == 1

        # 一批同时修正两条，恢复供数
        both = c.post("/scans/batch", json={"scans": [
            _scan("S2", 2, 2, "C:c2", "C:c1"),
            _scan("S3", 3, 2, "C:c1", None),
        ]})
        assert both.status_code == 200, both.text
        assert c.get("/health").json() == {"status": "ok"}
        assert c.get("/boxes/B:b1").json()["path"] == ["C:c1", "B:b1"]


def test_box_queries_share_committed_state_without_replaying(client):
    # 一次提交写入大量扫描；按盒查询只读已提交状态，路径自洽
    scans = [_scan(f"N{i}", i, 1, f"B:b{i}", "C:c1") for i in range(1, 81)]
    scans.append(_scan("N_ROOT", 81, 1, "C:c1", "P:p1"))
    assert client.post("/scans/batch", json={"scans": scans}).status_code == 200

    for i in (1, 40, 80):
        body = client.get(f"/boxes/B:b{i}").json()
        assert body["path"] == ["P:p1", "C:c1", f"B:b{i}"]

    # 另一个“进程”的客户端（新连接）看到同一已提交状态
    with TestClient(app) as other:
        assert other.get("/boxes/B:b40").json() == client.get("/boxes/B:b40").json()


# 子进程入口见 tests/_startup_worker.py：模拟同一卷上的另一个服务进程。


def test_stale_startup_check_cannot_suspend_after_other_process_committed_fix(db_file):
    _seed_raw(
        db_file,
        [
            ("S1", 1, 1, "B:b1", "C:c1"),
            ("S2", 2, 1, "C:c1", "C:c2"),
            ("S3", 3, 1, "C:c2", "C:c1"),
        ],
        edges=[("B:b1", "C:c1")],
    )
    env = {**os.environ, "TRACE_DB_PATH": str(db_file), "PYTHONPATH": os.getcwd()}
    worker = Path(__file__).with_name("_startup_worker.py")

    def run(*args):
        out = subprocess.run(
            [sys.executable, str(worker), *args],
            env=env, capture_output=True, text=True, timeout=60,
        )
        assert out.returncode == 0, out.stderr
        return out.stdout.strip().splitlines()[-1]

    first = json.loads(run("recover"))
    assert first["status"] == "conflict" and first["conflict_seq"] == 3

    # 另一进程提交有效更正并恢复供数
    fixed = json.loads(run("fix", json.dumps([_scan("S3", 3, 2, "C:c2", None)])))
    assert fixed["accepted"] is True
    assert json.loads(run("state"))["status"] == "ok"

    # 较早的启动校验结果不能再次停供：重新校验读到的是已提交更正
    again = json.loads(run("recover"))
    assert again["status"] == "ok"
    assert json.loads(run("state"))["status"] == "ok"


def test_conflict_state_is_shared_across_query_connections(db_file):
    """另一个进程的连接直接按盒查询：503 带冲突序号，而不是停供前的旧路径。"""

    _seed_raw(
        db_file,
        [
            ("S1", 1, 1, "B:b1", "C:c1"),
            ("S2", 2, 1, "C:c1", "C:c2"),
            ("S3", 3, 1, "C:c2", "C:c1"),
        ],
        edges=[("B:b1", "C:c1")],
    )
    with TestClient(app) as c:
        assert c.get("/health").status_code == 503

        # 直接另开连接（等价于同卷的另一个进程）读取已提交状态
        with connect() as conn:
            row = conn.execute(
                "SELECT status, conflict_seq FROM service_state WHERE id=1"
            ).fetchone()
        assert row["status"] == "conflict" and row["conflict_seq"] == 3


def test_startup_check_waits_for_inflight_commit_and_sees_fix(db_file):
    """启动校验在取得写锁后才读取：并发提交的有效更正不会被旧结果覆盖。"""

    import threading

    _seed_raw(
        db_file,
        [
            ("S1", 1, 1, "B:b1", "C:c1"),
            ("S2", 2, 1, "C:c1", "C:c2"),
            ("S3", 3, 1, "C:c2", "C:c1"),
        ],
    )

    holder = connect(str(db_file))
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("SELECT * FROM scans").fetchall()  # 读到冲突状态

    result: dict = {}

    def run_recover():
        from app.service import recover_on_startup

        result.update(recover_on_startup())

    t = threading.Thread(target=run_recover)
    t.start()
    # recover_on_startup 已启动但在等待写锁；此时提交有效更正
    import time

    time.sleep(0.3)
    holder.execute("UPDATE scans SET revision=2, parent=NULL WHERE scan_no='S3'")
    holder.execute("COMMIT")
    holder.close()

    t.join(timeout=30)
    assert not t.is_alive()
    # 启动校验看到的是已提交更正，不会再按旧结果停供
    assert result["status"] == "ok"
    with connect() as conn:
        assert conn.execute("SELECT status FROM service_state WHERE id=1").fetchone()[0] == "ok"


def test_legacy_volume_with_invalid_codes_is_suspended(db_file):
    """旧卷中的非法编码也按同一规则在启动时发现，并指出首个序号。"""

    _seed_raw(
        db_file,
        [
            ("S1", 1, 1, "B:b1", "C:c1"),
            ("S2", 2, 1, "X:bad", "C:c2"),  # 前缀非法
            ("S3", 3, 1, "C:c2", None),
        ],
    )
    with TestClient(app) as c:
        health = c.get("/health")
        assert health.status_code == 503
        assert health.json()["conflict_seq"] == 2
        assert c.get("/boxes/B:b1").status_code == 503


def test_compose_healthcheck_follows_service_state(db_file):
    """容器健康检查依赖 /health：停供时 503 使容器 unhealthy，恢复后 200。"""

    _seed_raw(
        db_file,
        [
            ("S1", 1, 1, "B:b1", "C:c1"),
            ("S2", 2, 1, "B:b1", "P:p1"),
        ],
    )
    with TestClient(app) as c:
        r = c.get("/health")
        assert r.status_code == 503
        assert r.json()["conflict_seq"] == 2
        fixed = c.post("/scans/batch", json={"scans": [
            _scan("S2", 2, 2, "C:c1", "P:p1"),
        ]})
        assert fixed.status_code == 200
        assert c.get("/health").status_code == 200
