"""SQLite 连接与建表。

持久化保证：扫描表、当前归属边表与服务状态表均在同一事务内写入，
进程重启后直接从已提交状态恢复，不会出现混合修订或半截层级。

并发保证：
* 写事务以 ``BEGIN IMMEDIATE`` 开启，跨进程串行化“读取 + 校验 + 写入”，
  同一卷上的多个进程不会基于同一旧状态各自提交。
* 查询使用只读事务取同一已提交快照（服务状态 + 归属边表），无需逐笔重放扫描。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

# 进程内写锁：减少同一进程内多个线程对 SQLite 写锁的争抢；
# 跨进程仍由 BEGIN IMMEDIATE 与 busy_timeout 串行化。
write_lock = threading.RLock()


def db_path() -> str:
    return os.environ.get("TRACE_DB_PATH", "/data/trace.db")


def connect(path: str | None = None) -> sqlite3.Connection:
    # isolation_level=None：关闭 sqlite3 的隐式事务，由下面显式 BEGIN/COMMIT，
    # 这样才能使用 BEGIN IMMEDIATE 在读取历史扫描前就拿到写锁。
    conn = sqlite3.connect(path or db_path(), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # WAL 允许读写并发；busy_timeout 让锁等待而不是立刻报 locked。
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    scan_no  TEXT PRIMARY KEY,            -- 扫描号唯一
    seq      INTEGER NOT NULL UNIQUE,     -- 有效扫描序号唯一
    revision INTEGER NOT NULL,            -- 该扫描号当前最高修订
    child    TEXT NOT NULL,
    parent   TEXT,                        -- NULL 表示拆出
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS edges (
    child  TEXT PRIMARY KEY,              -- 任一时点一个子项只能有一个父项
    parent TEXT NOT NULL
);

-- 服务供数状态：由启动校验或成功提交在同一事务内维护。
-- status='conflict' 时服务停供（/health 与按盒查询返回 503）。
CREATE TABLE IF NOT EXISTS service_state (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    status        TEXT NOT NULL,          -- 'ok' | 'conflict'
    conflict_seq  INTEGER,                -- 首个冲突的全线序号
    message       TEXT,
    generation    INTEGER NOT NULL DEFAULT 0,  -- 每次成功提交递增
    updated_at    TEXT NOT NULL
);
"""


def init_db(path: str | None = None) -> None:
    with write_lock:
        target = path or db_path()
        parent = os.path.dirname(os.path.abspath(target))
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = connect(target)
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """开启跨进程写事务（BEGIN IMMEDIATE）；异常回滚，正常结束提交。"""

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


@contextmanager
def read_transaction() -> Iterator[sqlite3.Connection]:
    """只读事务：多次 SELECT 看到同一已提交快照。"""

    conn = connect()
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
