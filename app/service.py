"""核心业务逻辑：扫描修订合并、按序号重放、层级校验与路径计算。

设计要点
--------
1. 每个扫描号只保留最高修订；同号同修订异文整批拒绝，低修订整批拒绝。
2. 所有生效扫描按 ``seq`` 全序重放，先在内存中构建归属边表，全部合法后
   才在同一事务内整体替换数据库边表并 upsert 扫描表 —— 任一步失败则
   回滚，旧扫描与层级保持不变，不会只更新上层箱而漏掉内盒。
3. 重放中发现的首个非法序号通过 ``ReplayConflict.conflict_seq`` 返回。
4. 启动时对库中全部生效扫描按序号做一次全量重放校验；校验失败则服务进入
   停供状态（``/health`` 与 ``/boxes/{box}`` 返回 503 并指出首个冲突序号），
   数据员仍可通过 ``POST /scans/batch`` 补传/更正。校验状态以数据库中已
   提交的扫描为准，多进程共享同一卷时以最后一次成功提交的结果为准，旧进程
   缓存的停供结论不会在别人修复后再次停供。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from .db import connect, transaction, write_lock
from .schemas import BatchIn, ScanIn


class BatchError(Exception):
    """批次自身的静态校验错误（HTTP 400）。"""

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


class ReplayConflict(Exception):
    """重放层级规则时的冲突（HTTP 409/503），带首个冲突序号。"""

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


def kind(code: str) -> str:
    """返回包装层级角色：'B' 盒 / 'C' 箱 / 'P' 托盘。"""

    return code[0]


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
    stored: List[sqlite3.Row], incoming: Optional[List[ScanIn]] = None
) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """合并已存扫描与本批扫描，按序号重放，返回 (有序扫描, 新归属边表)。

    ``incoming`` 为空时等价于只对库中已提交的生效扫描做全量重放，
    供启动校验使用。
    """

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
    for item in incoming or []:
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

    # 序号全局唯一（含历史扫描）
    seq_owner: Dict[int, str] = {}
    for rec in ordered:
        owner = seq_owner.get(rec["seq"])
        if owner is not None and owner != rec["scan_no"]:
            raise ReplayConflict(rec["seq"], f"序号 {rec['seq']} 已被扫描号 {owner} 占用")
        seq_owner[rec["seq"]] = rec["scan_no"]

    # 按序号重放层级
    edges: Dict[str, str] = {}
    for rec in ordered:
        child, parent, seq = rec["child"], rec["parent"], rec["seq"]
        if parent is None:
            # 拆出：删除当前归属（若本来就无归属则为空操作，仍合法）
            edges.pop(child, None)
            continue

        _check_role(seq, child, parent)
        _check_cycle(seq, edges, child, parent)
        # 直接覆盖：一个子项任一时点只能归属一处，旧归属被原子替换
        edges[child] = parent

    return ordered, edges


def _load_state(conn: sqlite3.Connection) -> Tuple[List[sqlite3.Row], Dict[str, str]]:
    stored = conn.execute("SELECT * FROM scans").fetchall()
    old_edges = {
        row["child"]: row["parent"]
        for row in conn.execute("SELECT child, parent FROM edges")
    }
    return stored, old_edges


def submit_batch(batch: BatchIn) -> Dict[str, Any]:
    """提交一批扫描；成功返回盒码路径变化，失败保证旧状态不变。

    停供期间本接口仍然开放：整批补传/更正通过全量重放且无冲突时，扫描、
    修复后的归属边表与恢复供数在同一事务内一起生效；任一校验失败则回滚，
    保留原扫描和归属并继续停供。
    """

    validate_batch(batch)

    with write_lock, transaction() as conn:
        stored, old_edges = _load_state(conn)
        ordered, new_edges = replay(stored, batch.scans)

        # 计算受影响盒码（盒只会作为子项出现）
        box_codes = sorted(
            {c for c, p in old_edges.items() if c.startswith("B:")}
            | {c for c, p in new_edges.items() if c.startswith("B:")}
        )
        changes = []
        for box in box_codes:
            old_path = path_of(old_edges, box) if box in old_edges else [box]
            new_path = path_of(new_edges, box) if box in new_edges else [box]
            if old_path == new_path:
                continue
            changes.append(
                {
                    "box": box,
                    "old_pallet": pallet_of(old_edges, box) if box in old_edges else None,
                    "new_pallet": pallet_of(new_edges, box) if box in new_edges else None,
                    "old_path": old_path,
                    "new_path": new_path,
                }
            )

        # 整体替换边表：删除与插入在同一事务内，读者要么看到旧层级要么看到新层级
        conn.execute("DELETE FROM edges")
        conn.executemany(
            "INSERT INTO edges(child, parent) VALUES (?, ?)",
            list(new_edges.items()),
        )
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
        # 与扫描/边表同一事务推进版本号：提交成功即代表库中生效扫描已可
        # 无冲突重放，其他进程凭版本号感知修复，不会沿用旧的停供结论。
        conn.execute(
            "INSERT INTO service_meta(key, value) VALUES('state_version', 1) "
            "ON CONFLICT(key) DO UPDATE SET value = value + 1"
        )
        return {"accepted": True, "replayed_scans": len(ordered), "changes": changes}


def get_box_path(box: str) -> Optional[Dict[str, Any]]:
    """按盒查询完整当前包装路径；盒从未出现或已彻底移除返回 None。"""

    if not box.startswith("B:"):
        raise BatchError("bad_box_code", f"{box} 不是 B: 开头的盒码")

    conn = connect()
    try:
        row = conn.execute("SELECT 1 FROM scans WHERE child = ? LIMIT 1", (box,)).fetchone()
        if row is None:
            return None
        edges = {
            r["child"]: r["parent"]
            for r in conn.execute("SELECT child, parent FROM edges")
        }
    finally:
        conn.close()

    if box not in edges:  # 最后一次生效操作是拆出
        return None

    path = path_of(edges, box)
    return {
        "box": box,
        "parent": edges.get(box),
        "pallet": pallet_of(edges, box),
        "path": path,
    }


# --------------------------------------------------------------------------- #
# 服务状态：启动校验、停供与多进程状态协调
# --------------------------------------------------------------------------- #
class ServiceStatus:
    """进程内缓存的服务状态，以库中 ``state_version`` 为失效依据。

    停供结论只在缓存版本仍等于数据库当前版本时有效：同一卷上的另一进程
    一旦提交有效更正（事务内推进版本号），本进程下一次查询即重新按库中
    有效扫描校验并恢复供数，旧的启动校验结果不会再次停供。
    """

    def __init__(self) -> None:
        self._lock = write_lock
        self._version: Optional[int] = None
        self._conflict_seq: Optional[int] = None
        self._reason: Optional[str] = None

    def _read_version(self, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT value FROM service_meta WHERE key = 'state_version'"
        ).fetchone()
        return int(row["value"]) if row is not None else 0

    def verify_on_startup(self) -> None:
        """启动时按全线序号校验旧 SQLite 卷中的全部生效扫描。"""

        with self._lock:
            conn = connect()
            try:
                stored = conn.execute("SELECT * FROM scans").fetchall()
                version = self._read_version(conn)
                try:
                    _, edges = replay(stored)
                except ReplayConflict as exc:
                    self._version = version
                    self._conflict_seq = exc.conflict_seq
                    self._reason = exc.reason
                    return
                # 旧卷可能从未写过版本号：校验通过后补记，且顺手把偏离
                # 重放结果的边表修正为权威归属（同一事务）。
                self._repair_if_drifted(conn, edges)
                if version == 0:
                    conn.execute(
                        "INSERT INTO service_meta(key, value) "
                        "VALUES('state_version', 1) "
                        "ON CONFLICT(key) DO UPDATE SET value = value + 1"
                    )
                    conn.commit()
                    version = self._read_version(conn)
                self._version = version
                self._conflict_seq = None
                self._reason = None
            finally:
                conn.close()

    @staticmethod
    def _repair_if_drifted(conn: sqlite3.Connection, edges: Dict[str, str]) -> None:
        """边表与重放结果不一致时，以重放结果整体替换（仅启动校验路径）。"""

        current = {
            r["child"]: r["parent"]
            for r in conn.execute("SELECT child, parent FROM edges")
        }
        if current != edges:
            conn.execute("DELETE FROM edges")
            conn.executemany(
                "INSERT INTO edges(child, parent) VALUES (?, ?)",
                list(edges.items()),
            )
            conn.commit()

    def current(self) -> "ServiceStatus":
        """返回与数据库已提交状态对齐后的当前状态（刷新过期缓存）。"""

        with self._lock:
            conn = connect()
            try:
                version = self._read_version(conn)
                if self._version is not None and version == self._version:
                    return self
                # 版本变化（另一进程提交了更正）或首次查询：按库中有效扫描重新确认
                stored = conn.execute("SELECT * FROM scans").fetchall()
                try:
                    _, edges = replay(stored)
                except ReplayConflict as exc:
                    self._version = version
                    self._conflict_seq = exc.conflict_seq
                    self._reason = exc.reason
                    return self
                self._repair_if_drifted(conn, edges)
                self._version = version
                self._conflict_seq = None
                self._reason = None
                return self
            finally:
                conn.close()

    @property
    def available(self) -> bool:
        return self._conflict_seq is None

    @property
    def conflict_seq(self) -> Optional[int]:
        return self._conflict_seq

    @property
    def reason(self) -> Optional[str]:
        return self._reason


status = ServiceStatus()
