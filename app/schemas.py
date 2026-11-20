"""请求/响应模型。

编码约定：
* ``B:`` 开头 —— 盒（最小包装，只能作为子项）
* ``C:`` 开头 —— 箱（可装入另一箱或托盘）
* ``P:`` 开头 —— 托盘（只能作为顶层父项，不能作为子项）
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

CODE_PATTERN = r"^[BCP]:\S+$"
MAX_BATCH_SIZE = 100


class ScanIn(BaseModel):
    """单条装箱扫描记录。"""

    scan_no: str = Field(..., min_length=1, description="扫描号，全局唯一")
    seq: int = Field(..., ge=1, description="全产线递增序号，有效扫描不得重复")
    revision: int = Field(..., ge=1, description="递增修订号，同扫描号只能增大")
    child: str = Field(..., pattern=CODE_PATTERN, description="子码（B: 盒 / C: 箱 / P: 托盘）")
    parent: Optional[str] = Field(
        ...,
        pattern=CODE_PATTERN,
        description="目标父码：非空=转入该包装并替换旧归属；null=拆出",
    )


class BatchIn(BaseModel):
    """一批扫描提交，至多 100 笔。"""

    scans: List[ScanIn] = Field(..., max_length=MAX_BATCH_SIZE)


class ChangeOut(BaseModel):
    """单个盒码的托盘归属变化。"""

    box: str
    old_pallet: Optional[str]
    new_pallet: Optional[str]
    old_path: List[str] = Field(..., description="变更前完整包装路径，自顶（托盘）向底（盒）")
    new_path: List[str] = Field(..., description="变更后完整包装路径，自顶（托盘）向底（盒）")


class BatchOut(BaseModel):
    accepted: bool
    replayed_scans: int = Field(..., description="重放后当前生效的扫描总数")
    changes: List[ChangeOut]


class BoxPathOut(BaseModel):
    box: str
    parent: Optional[str]
    pallet: Optional[str]
    path: List[str] = Field(..., description="完整当前包装路径，自顶（托盘）向底（盒）")


class HealthOut(BaseModel):
    status: str = Field(..., description="ok 表示库中生效扫描重放无冲突，服务供数")


class SuspendedOut(BaseModel):
    """停供响应（HTTP 503）：库中生效扫描重放冲突。"""

    error: str = "service_suspended"
    message: str
    conflict_seq: Optional[int] = Field(
        None, description="首个冲突的全线序号，补传/更正时优先修正它"
    )
    status: str = "conflict"
