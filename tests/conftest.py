"""测试夹具：每个测试使用独立临时 SQLite 库。"""

import os
import tempfile

import pytest

os.environ["TRACE_DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="trace-test-"), "test.db")

from app.db import connect, init_db  # noqa: E402
from app.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _database():
    init_db()
    yield


@pytest.fixture(autouse=True)
def _clean_tables():
    """每个测试前清空数据，保证用例互不干扰（结构保留）。"""

    conn = connect()
    try:
        conn.execute("DELETE FROM edges")
        conn.execute("DELETE FROM scans")
        conn.execute("DELETE FROM service_state")
        conn.commit()
    finally:
        conn.close()
    yield


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c
