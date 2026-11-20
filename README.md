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
* 写事务以 `BEGIN IMMEDIATE` 开启，跨进程串行化“读取已提交状态 + 校验 + 写入”；
  同一卷上的多个进程（多容器/多 worker）不会基于同一旧状态各自提交。
* 查询在只读事务内同时读取供数状态与归属边表，看到同一已提交快照，
  **无需逐笔重放扫描**；不会读到半截层级或混合修订。
* 数据落盘到 Docker 卷，重启后从已提交状态恢复。

## 启动校验与停供（503）

服务启动时（FastAPI lifespan）把库中全部生效扫描按**全线序号**重放一遍，
结果连同供数状态写入同一个事务：

| 启动校验结果 | 行为 |
| --- | --- |
| 重放无冲突 | 以重放结果修复归属边表，`GET /health` 返回 200，正常供数 |
| 重放有冲突 | 服务**停供**：`GET /health` 与 `GET /boxes/{box}` 均返回 **503**，并给出首个冲突序号 `conflict_seq`；扫描与归属原样保留（不返回旧路径） |

重放规则与提交时完全一致：序号全局唯一、角色合法、编码前缀合法（`B:/C:/P:`）、无环。

停供期间的恢复方式（数据员在 `/docs` 操作）：

```bash
# 1. 查看当前停供状态与首个冲突序号
curl -i http://localhost:8000/health
# HTTP/1.1 503
# {"error":"service_suspended","message":"...","conflict_seq":11,"status":"conflict"}

# 2. 用 POST /scans/batch 整批补传或更正（停供期间该接口照常可用）
curl -X POST http://localhost:8000/scans/batch \
  -H 'Content-Type: application/json' \
  -d '{"scans":[{"scan_no":"S011","seq":11,"revision":2,"child":"C:c1","parent":"P:p2"}]}'

# 3. 整批重放通过后，扫描、修复后的归属与恢复供数同一事务生效
curl http://localhost:8000/health   # => {"status":"ok"}
```

要点：

* 批次必须使**全部**生效扫描按原规则重放无冲突才生效；只修正第一处冲突而
  后续仍有冲突时，整批拒绝并继续停供。
* 提交失败（409/400）时保留原扫描与归属，服务继续停供，原 `conflict_seq` 不变。
* 恢复供数与扫描/归属写入在**同一事务**内完成：不会出现“扫描已修好但仍停供”
  或“已供数但归属还是旧的”。
* 同一卷上的多个进程共享同一份已提交状态：若另一进程已提交有效更正，
  较早的启动校验结果不会再次停供（启动校验在取得写锁后才读取状态）。

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
pytest          # 27 个端到端测试
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

服务停供（启动校验发现重放冲突）时返回 **503**，不返回停供前的旧路径：

```json
{"error": "service_suspended", "message": "装入 C:c1 会使 C:c2 的层级形成环",
 "conflict_seq": 3, "status": "conflict"}
```

### `GET /health` 健康检查

```json
{"status": "ok"}
```

停供时返回 **503**（同样的 `service_suspended` 结构，带首个冲突序号），
因此 Docker `HEALTHCHECK` 与编排层会据此把容器标为 unhealthy。重启后
健康状态完全由库中有效扫描的启动校验结果决定。

## 项目结构

```
app/
  main.py      FastAPI 路由与异常映射
  service.py   修订合并、按序号重放、环/角色/双归属校验、路径计算、启动校验与供数状态
  db.py        SQLite 连接、WAL、跨进程写事务、只读快照、建表
  schemas.py   Pydantic 请求/响应模型
tests/         端到端测试（含并发、重启恢复、启动停供/恢复、整批回滚）
Dockerfile
docker-compose.yml
```
