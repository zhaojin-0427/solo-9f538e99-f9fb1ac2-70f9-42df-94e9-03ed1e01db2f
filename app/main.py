"""FastAPI 入口：单条产线装箱扫描 API。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .db import init_db
from .schemas import BatchIn, BatchOut, BoxPathOut
from .service import (
    BatchError,
    ReplayConflict,
    RevisionConflict,
    get_box_path,
    status,
    submit_batch,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 启动时按全线序号校验旧 SQLite 卷中的全部生效扫描；
    # 校验失败进入停供状态，等待数据员补传/更正。
    init_db()
    status.verify_on_startup()
    yield


app = FastAPI(
    title="药品追溯装箱扫描 API",
    version="1.1.0",
    description=(
        "单条产线扫描提交与包装层级查询。每批至多 100 笔；非空父码表示转入并替换旧归属，"
        "空值表示拆出。冲突批次整体拒绝，原扫描与层级不变。"
        "启动时按全线序号重放校验卷中生效扫描，存在冲突时 /health 与 /boxes/{box} "
        "返回 503 停供，数据员仍可在 /docs 通过 POST /scans/batch 补传或更正。"
    ),
    lifespan=lifespan,
)


@app.exception_handler(BatchError)
def _handle_batch_error(_: Request, exc: BatchError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"error": exc.code, "message": exc.message, **exc.extra},
    )


@app.exception_handler(RevisionConflict)
def _handle_revision_conflict(_: Request, exc: RevisionConflict) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "error": "revision_conflict",
            "message": exc.reason,
            "scan_no": exc.scan_no,
            "conflict_seq": exc.seq,
        },
    )


@app.exception_handler(ReplayConflict)
def _handle_replay_conflict(_: Request, exc: ReplayConflict) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "error": "replay_conflict",
            "message": exc.reason,
            "conflict_seq": exc.conflict_seq,
        },
    )


def _service_unavailable() -> JSONResponse:
    """停供响应：指出首个冲突序号，提示可经 /scans/batch 补传或更正。"""

    state = status.current()
    return JSONResponse(
        status_code=503,
        content={
            "error": "service_unavailable",
            "message": (
                "库中生效扫描重放存在冲突，服务已停供；"
                f"首个冲突序号 {state.conflict_seq}：{state.reason}。"
                "请通过 POST /scans/batch 补传或更正，全量重放无冲突后自动恢复。"
            ),
            "conflict_seq": state.conflict_seq,
        },
    )


@app.get("/health")
def health() -> JSONResponse:
    state = status.current()
    if not state.available:
        return _service_unavailable()
    return JSONResponse(content={"status": "ok"})


@app.post("/scans/batch", response_model=BatchOut)
def submit(batch: BatchIn) -> dict:
    """提交一批扫描（≤100 笔），全部成功才生效，否则整批拒绝。

    停供期间仍可调用：整批补传/更正使全部生效扫描重放无冲突时，
    扫描、修复后的归属与恢复供数在同一事务内一起生效。
    """

    return submit_batch(batch)


@app.get("/boxes/{box:path}", response_model=BoxPathOut)
def box_path(box: str) -> JSONResponse:
    """按盒码查询完整当前包装路径（自顶向底）。停供期间返回 503。"""

    state = status.current()
    if not state.available:
        return _service_unavailable()

    result = get_box_path(box)
    if result is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": "box_not_found",
                "message": f"盒码 {box} 不存在或当前已拆出，无包装归属",
                "box": box,
            },
        )
    return JSONResponse(content=result)
