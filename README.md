# 药品追溯装箱扫描 API

单条产线的装箱扫描服务：FastAPI + SQLite。数据员通过批量扫描确认每盒药当前
所在托盘，支持包装层级（盒 → 箱 → 箱/托盘）的转入、拆出、补传与更正。

## 包装编码规则

| 前缀 | 含义 | 规则 |
| --- | --- | --- |
| `B:` | 盒（最小包装） | 只能作为子项，只能装入 `C:` 箱 |
| `C:` | 箱 | 可装入另一 `C:` 箱或 `P:` 托盘 |
| `P:` | 托盘 | 只能作顶层父项，**不能作为子项** |

* 非空父码：子项转入该包装，并**原子替换旧归属**（任一时点不会同时归属两处）。
* 父码为 `null`：把子项从当前包装**拆出**。

## 扫描与修订语义

* 每批最多 **100 笔**，字段：`scan_no`（扫描号）、`seq`（全产线序号）、
  `revision`（修订号）、`child`（子码）、`parent`（目标父码或 null）。
* 有效扫描的 `seq` 全局唯一（批次内重复直接 400；与历史冲突在重放时 409）。
* 每个 `scan_no` 只保留**最高修订**：
  * 新修订号必须 **大于** 已存修订，否则整批拒绝（409）；
  * 同号同修订但内容（序号/子码/父码）不一致，整批拒绝（409）；
  * 同号同修订同文重提为幂等空操作。
* 所有生效扫描按 `seq` 全序**重放**，逐笔重建层级；任一笔违反角色规则、
  形成环或序号冲突，整批拒绝并返回**首个冲突序号** `conflict_seq`，
  原扫描与层级保持不变。
* 补传/更正导致后续已存扫描变非法时，同样整批拒绝（重放覆盖全量历史）。

## 一致性保证

* 校验、重放、扫描表 upsert、归属边表整体替换在**同一个 SQLite 事务**内完成，
  失败即回滚，不会只更新上层箱而漏掉内盒。
* WAL 模式 + 进程内写锁串行化并发提交，读写并发下不会出现混合修订。
* 数据落盘到 Docker 卷，重启后从已提交状态恢复。

## 启动校验与停供恢复

服务启动时按全线 `seq` 序号对旧 SQLite 卷中的**全部生效扫描**做一次全量重放
校验（角色规则、环、序号唯一性），不再无条件报告健康：

* 重放无冲突：服务正常供数；若卷中边表与重放结果不一致（旧版本遗留的旧路径），
  启动时以重放结果在同一事务内整体修正边表。
* 重放发现冲突：服务进入**停供**状态，`GET /health` 与 `GET /boxes/{box}`
  一律返回 **503**，响应体指出**首个冲突序号** `conflict_seq`；`/docs` 与
  `POST /scans/batch` 仍可使用，数据员可补传或更正。
* 停供期间的整批补传/更正必须使**全部生效扫描**按原规则重放无冲突：校验通过后，
  扫描、修复后的归属边表与恢复供数在**同一事务**内一起生效；任一步失败则回滚，
  保留原扫描和归属，继续停供。
* 同卷多进程以同一已提交状态为准：每次成功提交在事务内推进 `state_version`，
  进程仅在版本号变化时重新按库中有效扫描确认状态，查询不逐笔重放扫描。另一进程
  已提交有效更正后，较早的启动停供结论自动失效，不会再次停供。
* 重启后始终按库中当前有效扫描重新确认服务状态。

### 停供响应示例（HTTP 503）

```json
{
  "error": "service_unavailable",
  "message": "库中生效扫描重放存在冲突，服务已停供；首个冲突序号 2：……。请通过 POST /scans/batch 补传或更正，全量重放无冲突后自动恢复。",
  "conflict_seq": 2
}
```

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

* API：<http://localhost:8000>
* 交互式文档（Swagger）：<http://localhost:8000/docs>
* OpenAPI：<http://localhost:8000/openapi.json>
* SQLite 数据持久化在命名卷 `trace-data`（容器内 `/data/trace.db`）。

## 本地开发（无 Docker）

```bash
pip install -r requirements.txt
pip install pytest httpx
TRACE_DB_PATH=./data/trace.db uvicorn app.main:app --reload
pytest          # 29 个端到端测试
```

## 接口

### `POST /scans/batch` 提交一批扫描

```json
{
  "scans": [
    {"scan_no": "S001", "seq": 1, "revision": 1, "child": "B:box01", "parent": "C:case01"},
    {"scan_no": "S002", "seq": 2, "revision": 1, "child": "C:case01", "parent": "P:pal01"},
    {"scan_no": "S003", "seq": 3, "revision": 1, "child": "B:box02", "parent": null}
  ]
}
```

成功响应（仅列出**路径发生变化的盒码**，含旧/新托盘与完整路径）：

```json
{
  "accepted": true,
  "replayed_scans": 3,
  "changes": [
    {
      "box": "B:box01",
      "old_pallet": null,
      "new_pallet": "P:pal01",
      "old_path": ["B:box01"],
      "new_path": ["P:pal01", "C:case01", "B:box01"]
    }
  ]
}
```

冲突响应（HTTP 409，指出首个冲突序号，整批未生效）：

```json
{"error": "replay_conflict", "message": "装入 C:c2 会使 C:c1 的层级形成环", "conflict_seq": 11}
```

| 状态码 | 场景 |
| --- | --- |
| 200 | 整批生效 |
| 400 | 空批次、批次内扫描号/序号重复 |
| 409 | 修订冲突、序号历史冲突、角色违规、环（带 `conflict_seq`） |
| 422 | 字段类型/编码前缀/批次超 100 笔等 schema 校验失败 |

`GET /health` 与 `GET /boxes/{box}` 额外可能返回 **503**：启动校验或状态刷新
发现库中生效扫描重放冲突时停供（带 `conflict_seq`），更正提交成功后自动恢复。

### `GET /boxes/{box}` 按盒查询当前完整包装路径

```bash
curl http://localhost:8000/boxes/B:box01
```

```json
{
  "box": "B:box01",
  "parent": "C:case01",
  "pallet": "P:pal01",
  "path": ["P:pal01", "C:case01", "B:box01"]
}
```

盒从未出现或最后一次生效操作为拆出时返回 404。

### `GET /health` 健康检查

正常返回 `200 {"status":"ok"}`；启动校验发现生效扫描重放冲突时返回
`503`（带首个冲突序号），Docker 健康检查据此标记容器 `unhealthy`，
但容器不退出，数据员仍可访问 `/docs` 提交补传/更正。

## 项目结构

```
app/
  main.py      FastAPI 路由、异常映射、停供 503 响应
  service.py   修订合并、按序号重放、环/角色/双归属校验、路径计算、启动校验与服务状态
  db.py        SQLite 连接、WAL、写锁、建表
  schemas.py   Pydantic 请求/响应模型
tests/         端到端测试（含并发、重启恢复、停供/修复、多进程状态协调、整批回滚）
Dockerfile
docker-compose.yml
```
