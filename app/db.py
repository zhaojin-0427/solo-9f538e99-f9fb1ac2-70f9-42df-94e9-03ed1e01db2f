"""SQLite 连接与建表。

持久化保证：扫描表与当前归属边表均在同一事务内写入，
进程重启后直接从已提交状态恢复，不会出现混合修订或半截层级。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

# 进程内写锁：SQLite 同一时刻只允许一个写事务，锁使并发提交串行化，
# 配合每个事务内的“校验 + 重放 + 整体替换边表”，保证不会产生混合修订。
write_lock = threading.RLock()


def db_path() -> str:
    return os.environ.get("TRACE_DB_PATH", "/data/trace.db")


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or db_path(), timeout=30.0)
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

CREATE TABLE IF NOT EXISTS service_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
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
            conn.commit()
        finally:
            conn.close()


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """开启写事务；异常回滚，正常结束提交。"""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
