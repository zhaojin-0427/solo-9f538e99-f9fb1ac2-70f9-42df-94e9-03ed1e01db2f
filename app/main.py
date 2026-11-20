"""FastAPI 入口：单条产线装箱扫描 API。

启动时（lifespan）按全线序号校验库中生效扫描：
* 合法 —— 以重放结果恢复归属并供数；
* 冲突 —— 服务停供，``GET /health`` 与 ``GET /boxes/{box}`` 返回 503，
  并指出首个冲突序号；数据员仍可通过 ``/docs`` 调用 ``POST /scans/batch``
  补传/更正，整批重放通过后恢复供数。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .db import init_db
from .schemas import BatchIn, BatchOut, BoxPathOut, HealthOut, SuspendedOut
from .service import (
    BatchError,
    ReplayConflict,
    RevisionConflict,
    ServiceSuspended,
    get_box_path,
    get_service_state,
    recover_on_startup,
    submit_batch,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    recover_on_startup()
    yield


app = FastAPI(
    title="药品追溯装箱扫描 API",
    version="1.1.0",
    description=(
        "单条产线扫描提交与包装层级查询。每批至多 100 笔；非空父码表示转入并替换旧归属，"
        "空值表示拆出。冲突批次整体拒绝，原扫描与层级不变。"
        "启动时按全线序号校验库中生效扫描；重放冲突则停供（健康检查与按盒查询 503），"
        "可用整批补传/更正恢复。"
    ),
    lifespan=lifespan,
)


def _suspended_response(
    conflict_seq, message: str, extra: dict | None = None
) -> JSONResponse:
    content = {
        "error": "service_suspended",
        "message": message,
        "conflict_seq": conflict_seq,
        "status": "conflict",
    }
    if extra:
        content.update(extra)
    return JSONResponse(status_code=503, content=content)


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


@app.exception_handler(ServiceSuspended)
def _handle_suspended(_: Request, exc: ServiceSuspended) -> JSONResponse:
    return _suspended_response(exc.conflict_seq, exc.message)


@app.get(
    "/health",
    response_model=HealthOut,
    responses={503: {"model": SuspendedOut, "description": "生效扫描重放冲突，服务停供"}},
)
def health():
    """健康检查：仅当库中生效扫描重放无冲突时返回 200。"""

    state = get_service_state()
    if state["status"] != "ok":
        return _suspended_response(
            state["conflict_seq"], state["message"] or "生效扫描重放冲突，服务停供"
        )
    return HealthOut(status="ok")


@app.post("/scans/batch", response_model=BatchOut)
def submit(batch: BatchIn) -> dict:
    """提交一批扫描（≤100 笔），全部成功才生效，否则整批拒绝。

    停供期间仍可调用：整批补传/更正须使全部生效扫描按原规则重放无冲突，
    成功后扫描、修复后的归属与恢复供数在同一事务内一起生效；失败则保留
    原扫描与归属，继续停供。
    """

    return submit_batch(batch)


@app.get(
    "/boxes/{box:path}",
    response_model=BoxPathOut,
    responses={503: {"model": SuspendedOut, "description": "生效扫描重放冲突，服务停供"}},
)
def box_path(box: str):
    """按盒码查询完整当前包装路径（自顶向底）；停供时返回 503。"""

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
    return result
