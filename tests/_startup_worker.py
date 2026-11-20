"""子进程入口：模拟同卷上的另一个服务进程。

用法：
    python tests/_startup_worker.py recover
    python tests/_startup_worker.py fix '<scans json>'
    python tests/_startup_worker.py state
"""

import json
import os
import sys

sys.path.insert(0, os.getcwd())

from app.db import connect, init_db
from app.schemas import BatchIn
from app.service import recover_on_startup, submit_batch


def main() -> None:
    mode = sys.argv[1]
    init_db()
    if mode == "recover":
        print(json.dumps(recover_on_startup()))
    elif mode == "fix":
        result = submit_batch(
            BatchIn.model_validate({"scans": json.loads(sys.argv[2])})
        )
        print(json.dumps({"accepted": result["accepted"]}))
    elif mode == "state":
        with connect() as conn:
            row = conn.execute(
                "SELECT status, conflict_seq FROM service_state WHERE id=1"
            ).fetchone()
        print(
            json.dumps(
                {"status": row["status"], "conflict_seq": row["conflict_seq"]}
                if row
                else None
            )
        )
    else:  # pragma: no cover
        raise SystemExit(f"unknown mode {mode}")


if __name__ == "__main__":
    main()
