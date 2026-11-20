"""核心业务逻辑：扫描修订合并、按序号重放、层级校验与路径计算。

设计要点
--------
1. 每个扫描号只保留最高修订；同号同修订异文整批拒绝，低修订整批拒绝。
2. 所有生效扫描按 ``seq`` 全序重放，先在内存中构建归属边表，全部合法后
   才在同一事务内整体替换数据库边表并 upsert 扫描表 —— 任一步失败则
   回滚，旧扫描与层级保持不变，不会只更新上层箱而漏掉内盒。
3. 重放中发现的首个非法序号通过 ``ReplayConflict.conflict_seq`` 返回。
4. 服务状态与扫描/归属同事务维护：启动时按库中生效扫描全量重放，重放
   失败则停供（``/health`` 与按盒查询 503，并给出首个冲突序号）；整批
   补传/更正成功后，扫描、归属与恢复供数在同一事务内一起生效。
5. 写事务使用 ``BEGIN IMMEDIATE``，跨进程串行化“读状态 + 校验 + 写入”，
   较晚提交者总是基于已提交的最新状态，旧启动校验结果不会覆盖有效更正。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from .db import read_transaction, transaction, write_lock
from .schemas import BatchIn, ScanIn


class BatchError(Exception):
    """批次自身的静态校验错误（HTTP 400）。"""

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


class ReplayConflict(Exception):
    """重放层级规则时的冲突（HTTP 409 或启动停供），带首个冲突序号。"""

    def __init__(self, conflict_seq: int, reason: str) -> None:
        super().__init__(reason)
        self.conflict_seq = conflict_seq
        self.reason = reason


class RevisionConflict(Exception):
    """扫描号修订冲突（HTTP 409）。"""

    def __init__(self, scan_no: str, seq: Optional[int], reason: str) -> None:
        super().__init__(reason)
        self.scan_no = scan_no
        self.seq = seq
        self.reason = reason


class ServiceSuspended(Exception):
    """库中生效扫描重放冲突，服务停供（HTTP 503）。"""

    def __init__(self, conflict_seq: Optional[int], message: str) -> None:
        super().__init__(message)
        self.conflict_seq = conflict_seq
        self.message = message


def kind(code: str) -> str:
    """返回包装层级角色：'B' 盒 / 'C' 箱 / 'P' 托盘。"""

    return code[0]


def _check_code(seq: int, code: str, label: str) -> None:
    """编码前缀必须合法；旧卷里的脏数据也按同一规则拒绝。"""

    if len(code) < 3 or code[0] not in "BCP" or code[1] != ":":
        raise ReplayConflict(seq, f"{label}编码不合法: {code}")


# --------------------------------------------------------------------------- #
# 层级工具函数（纯函数，便于测试）
# --------------------------------------------------------------------------- #
def path_of(edges: Dict[str, str], code: str) -> List[str]:
    """自顶（托盘/最外层）向底（该码）返回完整包装路径。"""

    chain = [code]
    cur = edges.get(code)
    seen = {code}
    while cur is not None:
        if cur in seen:  # 防御性：正常流程重放时已保证无环
            raise ReplayConflict(-1, "包装层级存在环")
        seen.add(cur)
        chain.append(cur)
        cur = edges.get(cur)
    chain.reverse()
    return chain


def pallet_of(edges: Dict[str, str], code: str) -> Optional[str]:
    """返回当前所在托盘（路径上首个 P: 节点），未上托盘返回 None。"""

    for node in path_of(edges, code):
        if node.startswith("P:"):
            return node
    return None


def validate_batch(batch: BatchIn) -> None:
    """批次内静态校验：非空、扫描号不重复、序号不重复。"""

    if not batch.scans:
        raise BatchError("empty_batch", "每批至少提交 1 笔扫描")

    scan_nos: Dict[str, ScanIn] = {}
    seqs: Dict[int, ScanIn] = {}
    for item in batch.scans:
        if item.scan_no in scan_nos:
            raise BatchError(
                "duplicate_scan_no",
                f"批次内扫描号重复: {item.scan_no}",
                scan_no=item.scan_no,
            )
        scan_nos[item.scan_no] = item

        if item.seq in seqs:
            raise BatchError(
                "duplicate_seq",
                f"批次内序号重复: {item.seq}",
                conflict_seq=item.seq,
            )
        seqs[item.seq] = item


def _check_role(seq: int, child: str, parent: str) -> None:
    """包装角色规则：盒只能入箱；箱可入箱/托盘；托盘不作子项。"""

    child_kind = kind(child)
    parent_kind = kind(parent)

    _check_code(seq, child, "子码")
    _check_code(seq, parent, "父码")
    if child_kind == "P":
        raise ReplayConflict(seq, f"托盘 {child} 不能作为子项装入 {parent}")
    if child_kind == "B" and parent_kind != "C":
        raise ReplayConflict(seq, f"盒 {child} 只能装入 C: 开头的箱，不能装入 {parent}")
    if child_kind == "C" and parent_kind not in ("C", "P"):
        raise ReplayConflict(seq, f"箱 {child} 只能装入另一箱或托盘，不能装入 {parent}")


def _check_cycle(seq: int, edges: Dict[str, str], child: str, parent: str) -> None:
    """若把 child 挂到 parent 下会形成环，则按当前序号拒绝。"""

    cur: Optional[str] = parent
    while cur is not None:
        if cur == child:
            raise ReplayConflict(seq, f"装入 {parent} 会使 {child} 的层级形成环")
        cur = edges.get(cur)


def replay(
    stored: List[sqlite3.Row], incoming: List[ScanIn]
) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """合并已存扫描与本批扫描，按序号重放，返回 (有序扫描, 新归属边表)。"""

    merged: Dict[str, Dict[str, Any]] = {
        row["scan_no"]: {
            "scan_no": row["scan_no"],
            "seq": row["seq"],
            "revision": row["revision"],
            "child": row["child"],
            "parent": row["parent"],
        }
        for row in stored
    }

    # 修订检查与合并
    for item in incoming:
        old = merged.get(item.scan_no)
        if old is not None:
            if item.revision < old["revision"]:
                raise RevisionConflict(
                    item.scan_no,
                    item.seq,
                    f"修订号必须递增：已存修订 {old['revision']}，收到 {item.revision}",
                )
            if item.revision == old["revision"] and (
                item.child != old["child"]
                or item.parent != old["parent"]
                or item.seq != old["seq"]
            ):
                raise RevisionConflict(
                    item.scan_no,
                    item.seq,
                    f"扫描号 {item.scan_no} 同修订 {item.revision} 内容不一致，整批拒绝",
                )
        merged[item.scan_no] = {
            "scan_no": item.scan_no,
            "seq": item.seq,
            "revision": item.revision,
            "child": item.child,
            "parent": item.parent,
        }

    ordered = sorted(merged.values(), key=lambda r: (r["seq"], r["scan_no"]))

    # 按序号全序重放：序号冲突、角色违规与环都在同一遍里检查，
    # 因此上报的总是全序中的首个冲突序号。
    edges: Dict[str, str] = {}
    seq_owner: Dict[int, str] = {}
    for rec in ordered:
        child, parent, seq = rec["child"], rec["parent"], rec["seq"]

        owner = seq_owner.get(seq)
        if owner is not None and owner != rec["scan_no"]:
            raise ReplayConflict(seq, f"序号 {seq} 已被扫描号 {owner} 占用")
        seq_owner[seq] = rec["scan_no"]

        if parent is None:
            # 拆出：删除当前归属（若本来就无归属则为空操作，仍合法）
            _check_code(seq, child, "子码")
            edges.pop(child, None)
            continue

        _check_role(seq, child, parent)
        _check_cycle(seq, edges, child, parent)
        # 直接覆盖：一个子项任一时点只能归属一处，旧归属被原子替换
        edges[child] = parent

    return ordered, edges


# --------------------------------------------------------------------------- #
# 服务状态（与扫描、归属同库同事务）
# --------------------------------------------------------------------------- #
OK = "ok"
SUSPENDED = "conflict"


def _read_state(conn: sqlite3.Connection) -> Dict[str, Any]:
    row = conn.execute(
        "SELECT status, conflict_seq, message, generation FROM service_state WHERE id = 1"
    ).fetchone()
    if row is None:
        # 尚未写入状态（例如首次建库）：默认供数，启动校验会立即落库。
        return {
            "status": OK,
            "conflict_seq": None,
            "message": None,
            "generation": 0,
        }
    return {
        "status": row["status"],
        "conflict_seq": row["conflict_seq"],
        "message": row["message"],
        "generation": row["generation"],
    }


def _state_to_dict(state: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": state["status"],
        "conflict_seq": state["conflict_seq"],
        "message": state["message"],
        "generation": state["generation"],
    }


def _mark_ok(conn: sqlite3.Connection) -> None:
    """标记供数正常并递增代次；与扫描/归属写入处于同一事务。"""

    conn.execute(
        """
        INSERT INTO service_state(id, status, conflict_seq, message, generation, updated_at)
        VALUES (1, 'ok', NULL, NULL, 1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        ON CONFLICT(id) DO UPDATE SET
            status = 'ok',
            conflict_seq = NULL,
            message = NULL,
            generation = service_state.generation + 1,
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        """
    )


def _mark_suspended(
    conn: sqlite3.Connection, conflict_seq: Optional[int], message: str
) -> None:
    """标记停供；不触碰 scans / edges，原扫描与归属保持不变。"""

    conn.execute(
        """
        INSERT INTO service_state(id, status, conflict_seq, message, generation, updated_at)
        VALUES (1, 'conflict', ?, ?, 0, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        ON CONFLICT(id) DO UPDATE SET
            status = 'conflict',
            conflict_seq = excluded.conflict_seq,
            message = excluded.message,
            updated_at = excluded.updated_at
        """,
        (conflict_seq, message),
    )


def _load_stored(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute("SELECT * FROM scans").fetchall()


def _load_edges(conn: sqlite3.Connection) -> Dict[str, str]:
    return {
        row["child"]: row["parent"]
        for row in conn.execute("SELECT child, parent FROM edges")
    }


def _replace_edges(conn: sqlite3.Connection, edges: Dict[str, str]) -> None:
    conn.execute("DELETE FROM edges")
    conn.executemany(
        "INSERT INTO edges(child, parent) VALUES (?, ?)",
        list(edges.items()),
    )


def recover_on_startup() -> Dict[str, Any]:
    """启动时按全线序号校验库中生效扫描，并以同一事务恢复供数状态。

    * 全部生效扫描重放合法：用重放结果修复归属边表，并标记供数正常；
    * 重放冲突：只写入停供状态（首个冲突序号），扫描与归属原样保留。

    整个“读取当前已提交状态 + 重放 + 写入”在 ``BEGIN IMMEDIATE`` 事务内完成。
    若另一进程已先提交有效更正，本进程读到的是更正后的扫描，不会用较早的
    校验结果再次停供；反之，本进程先提交停供后，另一进程的有效更正仍会
    随后恢复供数。
    """

    with write_lock, transaction() as conn:
        stored = _load_stored(conn)
        try:
            _, edges = replay(stored, [])
        except ReplayConflict as exc:
            _mark_suspended(conn, exc.conflict_seq, exc.reason)
            return {
                "status": SUSPENDED,
                "conflict_seq": exc.conflict_seq,
                "message": exc.reason,
            }

        # 以重放结果为准修复归属（例如旧卷中边表缺失/过期），再恢复供数。
        _replace_edges(conn, edges)
        _mark_ok(conn)
        return {"status": OK, "conflict_seq": None, "message": None}


def get_service_state() -> Dict[str, Any]:
    """返回已提交的服务状态（读取一致性快照）。"""

    with read_transaction() as conn:
        return _state_to_dict(_read_state(conn))


def _ensure_serving(conn: sqlite3.Connection) -> None:
    state = _read_state(conn)
    if state["status"] != OK:
        raise ServiceSuspended(state["conflict_seq"], state["message"] or "服务停供")


# --------------------------------------------------------------------------- #
# 提交与查询
# --------------------------------------------------------------------------- #
def _safe_path(edges: Dict[str, str], code: str) -> List[str]:
    """计算路径；停供期间旧边表可能本身成环，退化为仅返回该码。"""

    try:
        return path_of(edges, code)
    except ReplayConflict:
        return [code]


def _safe_pallet(edges: Dict[str, str], code: str) -> Optional[str]:
    try:
        return pallet_of(edges, code)
    except ReplayConflict:
        return None


def submit_batch(batch: BatchIn) -> Dict[str, Any]:
    """提交一批扫描；成功返回盒码路径变化，失败保证旧扫描、归属与状态不变。"""

    validate_batch(batch)

    with write_lock, transaction() as conn:
        stored = _load_stored(conn)
        old_edges = _load_edges(conn)
        ordered, new_edges = replay(stored, batch.scans)

        # 计算受影响盒码（盒只会作为子项出现）
        box_codes = sorted(
            {c for c, p in old_edges.items() if c.startswith("B:")}
            | {c for c, p in new_edges.items() if c.startswith("B:")}
        )
        changes = []
        for box in box_codes:
            old_path = _safe_path(old_edges, box) if box in old_edges else [box]
            new_path = path_of(new_edges, box) if box in new_edges else [box]
            if old_path == new_path:
                continue
            changes.append(
                {
                    "box": box,
                    "old_pallet": _safe_pallet(old_edges, box) if box in old_edges else None,
                    "new_pallet": pallet_of(new_edges, box) if box in new_edges else None,
                    "old_path": old_path,
                    "new_path": new_path,
                }
            )

        # 整体替换边表：删除与插入在同一事务内，读者要么看到旧层级要么看到新层级
        _replace_edges(conn, new_edges)
        # upsert 本批扫描（同号同修订重提为幂等空写）
        conn.executemany(
            """
            INSERT INTO scans(scan_no, seq, revision, child, parent)
            VALUES (:scan_no, :seq, :revision, :child, :parent)
            ON CONFLICT(scan_no) DO UPDATE SET
                seq = excluded.seq,
                revision = excluded.revision,
                child = excluded.child,
                parent = excluded.parent,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            """,
            [item.model_dump() for item in batch.scans],
        )
        # 全量重放已通过：扫描、归属与恢复供数同一事务生效。
        _mark_ok(conn)
        return {"accepted": True, "replayed_scans": len(ordered), "changes": changes}


def get_box_path(box: str) -> Optional[Dict[str, Any]]:
    """按盒查询完整当前包装路径；盒从未出现或已彻底移除返回 None。

    服务状态与归属边表在同一只读快照内读取：若库中生效扫描未通过校验，
    抛出 ``ServiceSuspended``（503），不会返回停供前的旧路径。
    """

    with read_transaction() as conn:
        # 先确认供数状态：停供时一律 503，绝不返回旧路径。
        _ensure_serving(conn)
        if not box.startswith("B:"):
            raise BatchError("bad_box_code", f"{box} 不是 B: 开头的盒码")
        row = conn.execute(
            "SELECT 1 FROM scans WHERE child = ? LIMIT 1", (box,)
        ).fetchone()
        if row is None:
            return None
        edges = _load_edges(conn)

    if box not in edges:  # 最后一次生效操作是拆出
        return None

    path = path_of(edges, box)
    return {
        "box": box,
        "parent": edges.get(box),
        "pallet": pallet_of(edges, box),
        "path": path,
    }
