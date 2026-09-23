# 插件依赖兼容性审计 API

一个基于 **FastAPI + SQLite** 的插件依赖兼容性审计服务：

- 客户端提交插件清单（插件版本 + 依赖名 + SemVer 版本范围）；
- 服务解析**传递依赖**，在版本注册表上搜索可行的版本组合（回溯 + MRV 启发式）；
- **无解时不会只报第一个冲突**：遍历整棵搜索树，汇总所有本质不同的死路，
  对每条死路的约束集合做贪心极小化，返回**最小冲突链**以及链上**每一条约束的逐跳来源**；
- 所有审计结果持久化到 SQLite，支持按 ID 查询、列表过滤；
- 支持**异步作业模式**（`POST /api/v1/audit-jobs`）：批量提交立即返回 job_id，
  后台按序求解落库，可查询进度/逐项结果、取消排队项，服务重启后依据 SQLite 状态自动恢复；
- 自带健康检查、OpenAPI 文档、示例注册表与一键 Docker Compose 启动。

## 一条命令启动

```bash
docker compose up --build
```

启动后：

| 用途 | 地址 |
| --- | --- |
| API 服务 | <http://localhost:8000> |
| Swagger UI（可直接发请求） | <http://localhost:8000/docs> |
| 健康检查 | <http://localhost:8000/health> |

首次启动会由容器入口脚本 `entrypoint.sh` 先执行一次性初始化（`python -m app.bootstrap`）：
自动建表、把 `data/seed_registry.json` 里的示例包注册表写入 SQLite 卷（仅当注册表为空时播种）、
并把上次异常退出时遗留的“运行中”异步作业项标记为 `failed(interrupted)`，随后再启动 uvicorn。

本地直接运行（需要 Python 3.11）：

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
# 数据库默认位于 ./data/audits.db
```

## 快速上手

### 1. 提交一份可行清单

```bash
curl -s -X POST http://localhost:8000/api/v1/audits \
  -H 'Content-Type: application/json' \
  -d '{"plugin_name":"megaplugin","plugin_version":"1.0.0","dependencies":[]}' | jq
```

关键返回字段：

```json
{
  "id": "…",
  "status": "feasible",
  "resolution": {
    "megaplugin": "1.0.0",
    "authkit": "1.2.0",
    "cachekit": "1.3.0",
    "core-lib": "1.5.0",
    "httpkit": "2.2.0",
    "loggerkit": "0.5.0"
  },
  "sources": [ /* 每个被选中版本都列出全部约束及来源链 */ ]
}
```

### 2. 提交一份无解清单（返回全部最小冲突链）

```bash
curl -s -X POST http://localhost:8000/api/v1/audits \
  -H 'Content-Type: application/json' \
  -d '{"dependencies":[
    {"name":"cachekit","range":"^1.0.0"},
    {"name":"loggerkit","range":"^0.9.0"}
  ]}' | jq '.conflicts[0]'
```

示例（节选）：

```json
{
  "package": "core-lib",
  "kind": "selection_conflict",
  "locked_version": "2.0.0",
  "message": "core-lib 已被锁定为 2.0.0，但该版本不满足来自 cachekit@1.3.0 的约束 …",
  "minimal_constraints": [
    {
      "package": "core-lib",
      "version_range": "^1.4.0",
      "source_chain": [
        {"package": "<manifest>", "version_range": "*", "version": null},
        {"package": "cachekit",  "version_range": "=1.3.0", "version": "1.3.0"}
      ]
    },
    {
      "package": "core-lib",
      "version_range": "^2.0.0",
      "source_chain": [
        {"package": "<manifest>", "version_range": "*", "version": null},
        {"package": "loggerkit", "version_range": "=0.9.0", "version": "0.9.0"}
      ]
    }
  ]
}
```

要点：

- `conflicts` 是**所有**本质不同冲突的列表（上限 50 条），按包名/类型/链长排序；
- `minimal_constraints` 是贪心极小化后的不可满足子集（去掉任何一条就可能消解冲突）；
- `all_constraints` 保留该死路点上该包的全部约束，便于诊断；
- `source_chain` 从 `<manifest>` 根跳开始，逐跳记录“谁的哪个版本要求了什么范围”，传递依赖来源完整可追溯。

### 3. 查询审计结果

```bash
# 列表（可选 status=feasible|infeasible|inconclusive、limit、offset）
curl -s 'http://localhost:8000/api/v1/audits?status=infeasible' | jq

# 详情
curl -s http://localhost:8000/api/v1/audits/<id> | jq
```

### 4. 批量审计（1～20 个清单，逐项独立落库）

一次提交 1～20 个清单，服务**按输入顺序**对每个清单独立运行求解器并分别持久化：
单个清单校验失败、根插件不存在或求解异常**都不会阻止其他项**，
响应逐项返回 `audit_id` + `status`（成功）或结构化 `error`（失败），
并给出 `total` / `feasible` / `infeasible` / `inconclusive` / `error` 计数。

```bash
curl -s -X POST http://localhost:8000/api/v1/audits/batch \
  -H 'Content-Type: application/json' \
  -d '{"manifests":[
    {"dependencies":[{"name":"core-lib","range":"^1.0.0"}]},
    {"dependencies":[{"name":"cachekit","range":"^1.0.0"},{"name":"loggerkit","range":"^0.9.0"}]},
    {"dependencies":[{"name":"x","range":"not-valid"}]}
  ]}' | jq
```

返回（节选）：

```json
{
  "total": 3,
  "feasible": 1,
  "infeasible": 1,
  "inconclusive": 0,
  "error": 1,
  "items": [
    {
      "index": 0,
      "audit_id": "…",
      "status": "feasible",
      "audit": { "id": "…", "status": "feasible", "resolution": {"core-lib": "1.5.0"} },
      "error": null
    },
    {
      "index": 1,
      "audit_id": "…",
      "status": "infeasible",
      "audit": { "id": "…", "status": "infeasible", "conflicts": [ … ] },
      "error": null
    },
    {
      "index": 2,
      "audit_id": null,
      "status": null,
      "audit": null,
      "error": {
        "code": "invalid_manifest",
        "message": "清单校验失败",
        "details": [ {"loc": ["dependencies", 0, "range"], "msg": "非法版本范围 'not-valid' …"} ]
      }
    }
  ]
}
```

要点：

- **逐项隔离**：每个元素单独做 Pydantic 校验（元素不是对象、字段类型错误等也只挂在该项），
  根插件版本缺失、求解器异常、落库异常都仅记为该项错误，其余项照常处理；
- **成功项与单条接口完全等价**：各自生成独立 `audit_id`、独立事务落库，
  之后可用 `GET /api/v1/audits/{id}` 取回，`audit` 字段与单条响应同构；
- **失败项不产生审计记录**；错误码：
  `invalid_manifest`（清单非法/根插件缺失，携带 Pydantic `details`）、
  `solver_error`（求解内部异常）、`internal_error`（持久化异常）；
- `items` 顺序与请求 `manifests` 完全一致；
- 仅当整个批量请求**为空列表或超过 20 项**（或缺字段）时返回 `422`，此时不落任何记录；
  单项问题一律返回 `200` 并在该项的 `error` 中体现。

### 5. 异步审计作业：立即返回 job_id，后台按序求解落库

当清单较多或希望异步处理时，向 `POST /api/v1/audit-jobs` 提交**单条或批量**清单，
服务**立即返回 `job_id`（HTTP `202 Accepted`）**，后台单工作线程按提交顺序、
作业内顺序逐项求解，并以**每项独立事务**持久化：单项清单非法、根插件不存在、
求解异常或落库异常都**只把该项标记为 failed，不会阻断其他项**。

请求体支持三种形态（单条对象、裸数组、`manifests` 包装，批量上限 20 项）：

```bash
# 单条清单对象
curl -s -X POST http://localhost:8000/api/v1/audit-jobs \
  -H 'Content-Type: application/json' \
  -d '{"dependencies":[{"name":"core-lib","range":"^1.0.0"}]}'

# 批量：{"manifests": [...]}（也可以直接提交数组）
curl -s -X POST http://localhost:8000/api/v1/audit-jobs \
  -H 'Content-Type: application/json' \
  -d '{"manifests":[
    {"dependencies":[{"name":"core-lib","range":"^1.0.0"}]},
    {"dependencies":[{"name":"cachekit","range":"^1.0.0"},{"name":"loggerkit","range":"^0.9.0"}]},
    {"dependencies":[{"name":"x","range":"not-valid"}]}
  ]}' | jq '{id, status, total}'
```

即时响应（HTTP 202）：

```json
{
  "id": "8f1c…",
  "status": "queued",
  "total": 3,
  "items": [ {"index": 0, "status": "queued", "audit_id": null, "audit": null, "error": null, "manifest": {…}} ]
}
```

轮询作业详情：

```bash
curl -s http://localhost:8000/api/v1/audit-jobs/<job_id> | jq
```

处理完成后返回（节选）：

```json
{
  "id": "8f1c…",
  "status": "failed",
  "created_at": "…",
  "updated_at": "…",
  "total": 3,
  "completed": 3,
  "succeeded": 2,
  "failed": 1,
  "cancelled": 0,
  "queued": 0,
  "running": 0,
  "items": [
    {"index": 0, "status": "succeeded", "audit_id": "…", "audit": {"id": "…", "status": "feasible", …}, "error": null},
    {"index": 1, "status": "succeeded", "audit_id": "…", "audit": {"id": "…", "status": "infeasible", …}, "error": null},
    {"index": 2, "status": "failed", "audit_id": null, "audit": null,
     "error": {"code": "invalid_manifest", "message": "清单校验失败", "details": [ … ]}}
  ]
}
```

**作业状态**：

| status | 含义 |
| --- | --- |
| `queued` | 作业已创建，后台尚未开始处理第一项 |
| `running` | 至少一项在处理中，或仍有排队项 |
| `succeeded` | 全部项已终结且**没有失败项**（全部 succeeded，或全部被取消等无 failed 的情形） |
| `failed` | 已终结，且至少一项为 `failed`（清单非法/求解异常/落库异常/重启中断） |

单项状态：`queued` → `running` → `succeeded` / `failed`；`queued` 项可经 DELETE 变为 `cancelled`。
成功项与同步接口完全等价：独立 `audit_id`、独立事务落库，可用
`GET /api/v1/audits/{id}` 取回；失败项不产生审计记录，错误码为
`invalid_manifest`（携带 Pydantic `details`）、`solver_error`、`internal_error`，
或服务重启导致的 `interrupted`。

**取消尚未开始的项**（`running` 与已终结项不可取消）：

```bash
# 取消整个作业中全部 queued 项（返回本次取消数量）
curl -s -X DELETE http://localhost:8000/api/v1/audit-jobs/<job_id> | jq '{status,cancelled}'

# 仅取消作业中某个按下标的 queued 项
curl -s -X DELETE http://localhost:8000/api/v1/audit-jobs/<job_id>/items/2
```

取消语义与状态码：

- 作业不存在：`GET` / `DELETE` 返回 `404`；取消下标越界的单项返回 `404`；
- 项已开始或已终结（`running`/`succeeded`/`failed`/`cancelled`）：返回 `409`；
- 作业已无 queued 项时整体 DELETE：返回 `409`；
- 取消后仍有 `succeeded`/`failed` 项时按正常规则终结；若作业尚未开始就全部取消，
  则以 `succeeded` 终结（无任何失败项），`cancelled` 计数反映取消数量。

**重启恢复**：作业与每项状态全部持久化在 SQLite（`audit_jobs` / `audit_job_items`
两张表）。服务重启时，容器入口先跑 `python -m app.bootstrap`（幂等，本地可直接执行）：
上次进程遗留的 `running` 项被标记为 `failed`（`error_code=interrupted`），
随后启动的后台工作线程会自动继续处理所有剩余 `queued` 项直至作业终结。

入参校验：空数组、超过 20 项、数组元素不是清单对象、请求体不是合法 JSON
等**整体结构问题**返回 `422`（不创建作业）；单个清单内容是否合法在后台逐项校验，
只体现在该项的 `failed`/`error` 上。

### 6. 历史影响反查：某个包/版本被哪些审计依赖过

扫描**已持久化**审计结果的 `resolution`、`sources` 与 `conflicts`，沿来源链找出
直接或传递依赖该包（可选精确版本）的审计记录：

```bash
# 按包聚合全部版本
curl -s http://localhost:8000/api/v1/audits/impact/core-lib | jq

# 精确版本 + 仅看无解记录 + 限制条数
curl -s 'http://localhost:8000/api/v1/audits/impact/core-lib?version=2.0.0&status=infeasible&limit=10' | jq
```

返回（节选）：

```json
{
  "package": "core-lib",
  "version": null,
  "limit": 20,
  "total": 2,
  "summary": {
    "total": 2,
    "feasible": 1,
    "infeasible": 1,
    "inconclusive": 0,
    "resolution_hits": 1,
    "conflict_hits": 1,
    "root": 0,
    "direct": 1,
    "transitive": 1
  },
  "versions": [
    {"version": "1.5.0", "total": 1, "feasible": 1, "infeasible": 0},
    {"version": "2.0.0", "total": 1, "feasible": 0, "infeasible": 1}
  ],
  "items": [
    {
      "audit_id": "…",
      "status": "infeasible",
      "root_plugin": null,
      "depth": 1,
      "direct": true,
      "as_root": false,
      "versions": ["2.0.0"],
      "match_types": ["conflict"],
      "resolution_match": false,
      "conflict_match": true,
      "conflict_kinds": ["selection_conflict"],
      "matches": [
        {
          "match_type": "conflict",
          "role": "constrained",
          "depth": 1,
          "direct": true,
          "version": "2.0.0",
          "version_range": "^2.0.0",
          "conflict_kind": "selection_conflict",
          "conflict_package": "core-lib",
          "locked_version": "2.0.0",
          "minimal_chain": true,
          "source_chain": [
            {"package": "<manifest>",  "version_range": "*", "version": null},
            {"package": "loggerkit",  "version_range": "=0.9.0", "version": "0.9.0"}
          ]
        }
      ]
    }
  ]
}
```

要点：

- **命中层级 `depth`**：`0`=根插件自身，`1`=清单/根插件的直接依赖，`>=2`=传递依赖
  （来源链每多一跳 +1）；每条审计取其全部命中中的最小层级；
- 排序稳定：先按 `depth` 升序（直接依赖在前），同层按 `created_at` 倒序，审计 ID 兜底；
- **两类命中分开标注**：`resolution_match` / `match_type=resolution` 表示可行解中被选中，
  `conflict_match` / `match_type=conflict` 表示无解审计中的冲突相关命中；
  每条 `matches` 还区分 `role`：`resolved`（被解出）、`constrained`（作为冲突对象被约束）、
  `requester`（仅出现在某条约束的来源链上，沿链传递参与冲突），并用 `minimal_chain`
  标记是否属于最小冲突链；
- `range_conflict` 与 `package_missing` 没有具体入选版本，按包聚合时以 `version: null` 命中，
  指定 `version` 反查时不命中；`selection_conflict` 按 `locked_version` 命中；
- `versions` 在未指定 `version` 时按 SemVer 聚合各版本的 feasible/infeasible 计数；
  `summary` 的计数覆盖**全部命中记录（不受 limit 影响）**；
- **未知包返回 `200` 空结果（`total=0`、`items=[]`），不返回 404**；
  非法 SemVer 版本参数返回 `422`。

### 7. 导出可验证审计包并校验

把**按创建时间排序**的 `audit_id` 列表（1～100 个、不可重复）交给导出接口，
得到确定性的 JSONL 导出包：每条记录是规范化 JSON（键排序、紧凑、UTF-8），
附带逐行 `sha256`、整体 `payload_sha256`、清单摘要 `manifest_sha256`，
以及整个包 gzip 后的 Base64。导出与校验均为只读，不写入数据库。

```bash
# include_conflicts=false（默认）时行内不含 conflicts 字段
curl -s -X POST http://localhost:8000/api/v1/audits/export-bundle \
  -H 'Content-Type: application/json' \
  -d '{"audit_ids":["<id-早>","<id-晚>"],"include_conflicts":true}' \
  | jq '{record_count, manifest_sha256, records, jsonl}'
```

返回（节选）：

```json
{
  "record_count": 2,
  "manifest_sha256": "…",
  "records": [
    {"audit_id": "…", "sha256": "…（对该行字节）"},
    {"audit_id": "…", "sha256": "…"}
  ],
  "jsonl": "{…id=早…}\n{…id=晚…}\n",
  "manifest": {
    "format": "audit-jsonl-bundle",
    "format_version": "1.0",
    "include_conflicts": true,
    "record_count": 2,
    "payload_sha256": "…（对整个 JSONL 字节串）",
    "records": [ …同上… ],
    "manifest_sha256": "…（对清单自身规范化 JSON 的摘要）"
  },
  "gzip_base64": "…（信封 JSON 经 gzip、再 Base64，确定性压缩 mtime=0）"
}
```

同样的输入每次导出的 `jsonl` / `manifest_sha256` / `gzip_base64` 字节完全一致。
错误码：未知 ID 返回 `404`；重复 ID、列表为空或超过 100 个返回 `422`。

校验时直接回传 `gzip_base64`（也可以分别提交 `jsonl` + `manifest`）：

```bash
curl -s -X POST http://localhost:8000/api/v1/audits/verify-bundle \
  -H 'Content-Type: application/json' \
  -d "{\"gzip_base64\":\"$BUNDLE_B64\"}" | jq
```

通过时：

```json
{"verified": true, "first_error_index": -1, "error_type": null, "error_detail": null}
```

任何字节或记录顺序被改动都会失败，`first_error_index` 指向第一条出错的记录
（错误不针对记录时为 `-1`），`error_type` 给出错误类别：

| error_type | 含义 |
| --- | --- |
| `bundle_base64_invalid` / `bundle_gzip_invalid` / `bundle_envelope_invalid` | Base64 / gzip / 信封无法解码 |
| `malformed_manifest` | 清单缺字段或字段类型错误 |
| `manifest_digest_mismatch` | `manifest_sha256` 与清单内容不一致（清单被改动） |
| `unsupported_format` | 格式名或版本不支持 |
| `malformed_jsonl` | JSONL 不是字符串、未以换行结尾或含空行 |
| `record_count_mismatch` | JSONL 行数与清单记录数不一致 |
| `record_parse_error` | 某行不是合法 JSON |
| `record_id_missing` / `record_order_mismatch` | 记录缺少 id / id 与清单顺序不符（**顺序被改动**） |
| `record_digest_mismatch` | 某行 sha256 与清单不符（**行字节被改动**） |
| `conflicts_flag_mismatch` | 行内 conflicts 字段与 `include_conflicts` 标志不一致 |
| `payload_digest_mismatch` | `payload_sha256` 与整体 JSONL 不一致（换行等跨行字节改动） |

本地离线校验（不依赖服务端、不需要数据库）也可以直接用标准库：

```python
import base64, gzip, hashlib, json

def verify(gzip_base64):
    env = json.loads(gzip.decompress(base64.b64decode(gzip_base64)))
    m = env["manifest"]
    body = {k: v for k, v in m.items() if k != "manifest_sha256"}
    canon = lambda o: json.dumps(o, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":")).encode()
    assert hashlib.sha256(canon(body)).hexdigest() == m["manifest_sha256"]   # 清单
    lines = env["jsonl"].encode()[:-1].split(b"\n")
    assert len(lines) == m["record_count"]                                  # 记录数
    for i, line in enumerate(lines):
        assert json.loads(line)["id"] == m["records"][i]["audit_id"]        # 顺序
        assert hashlib.sha256(line).hexdigest() == m["records"][i]["sha256"]  # 逐条摘要
    assert hashlib.sha256(env["jsonl"].encode()).hexdigest() == m["payload_sha256"]  # 整体
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET`  | `/health` | 健康检查（含数据库连通性与注册表包数量） |
| `POST` | `/api/v1/audits` | 提交清单进行审计，返回求解结果并落库（`201`） |
| `POST` | `/api/v1/audits/batch` | 批量审计 1～20 个清单：按序独立求解并分别落库，逐项返回 audit_id/status 或结构化 error（`200`；空列表或超过 20 项返回 `422`） |
| `POST` | `/api/v1/audit-jobs` | 创建异步审计作业：接收单条清单对象、清单数组或 `{"manifests":[...]}`（1～20 项），立即返回 job_id（`202`）；结构非法返回 `422` |
| `GET`  | `/api/v1/audit-jobs/{id}` | 查询作业：`queued`/`running`/`succeeded`/`failed` 状态、完成计数（succeeded/failed/cancelled/queued/running）与逐项结果；不存在返回 `404` |
| `DELETE` | `/api/v1/audit-jobs/{id}` | 取消作业内全部尚未开始（queued）的项；作业不存在 `404`，无 queued 项 `409` |
| `DELETE` | `/api/v1/audit-jobs/{id}/items/{index}` | 取消某个 queued 单项；作业/下标不存在 `404`，已开始或已终结 `409` |
| `GET`  | `/api/v1/audits` | 审计记录列表，支持 `status` / `limit` / `offset` |
| `GET`  | `/api/v1/audits/{id}` | 查询单条审计的完整结果 |
| `GET`  | `/api/v1/audits/impact/{package_name}` | 历史影响反查：扫描 resolution/sources/conflicts，找直接或传递依赖该包/版本的审计（可选 `version` / `status` / `limit`），未知包返回空结果而非 404 |
| `POST` | `/api/v1/audits/export-bundle` | 把已有审计结果打包为确定性、可验证的 JSONL 导出包（只读，不写库） |
| `POST` | `/api/v1/audits/verify-bundle` | 校验导出包：manifest、记录顺序、逐条摘要、整体摘要（不写库） |
| `GET`  | `/api/v1/registry/packages` | 查看注册表全部包与版本 |
| `PUT`  | `/api/v1/registry/packages/{name}` | 新增/覆盖一个包及其版本依赖 |

### 清单格式

```json
{
  "plugin_name": "可选，根插件名（需存在于注册表）",
  "plugin_version": "可选，与 plugin_name 同时提供，SemVer",
  "dependencies": [
    {"name": "authkit", "range": "^1.0.0"}
  ]
}
```

- 根插件在注册表中声明的依赖会作为传递依赖自动纳入解析；
- `dependencies` 是清单直接声明的根约束；
- 状态码：单条接口非法 SemVer / 范围 / 根插件缺失返回 `422`，审计记录不存在返回 `404`；
  批量接口中这类问题只作为该项的 `invalid_manifest` 错误返回（整体仍为 `200`），
  仅空列表 / 超过 20 项 / 缺字段时整体返回 `422`。

### 支持的范围语法（npm 风格）

比较器 `>= > <= < =`、caret `^`、tilde `~`、X 范围（`1.x`、`1.2.*`、`*`）、
hyphen（`1.0.0 - 2.0.0`）、OR（`||`）、AND（空格），以及 SemVer 预发布可见性规则。

### 审计状态

- `feasible`：存在一组满足全部传递约束的版本，返回 `resolution` 与 `sources`；
- `infeasible`：搜索树已穷尽，无可行组合，返回全部最小冲突链；
- `inconclusive`：搜索节点超过预算（默认 20000），未能在预算内判定。

## 求解与冲突报告原理

- 搜索算法：约束传播 + MRV（候选交集最小的包优先）+ 时间顺序回溯，遍历完整搜索树；
- 冲突收集：每个死路记录一次，按“冲突类型 + 包 + 最小约束签名”去重；
- 极小化：对死路约束集合做逐个删除的贪心化简（range 冲突求空交集最小子集；
  锁定冲突求仍排除已锁版本的最小子集），保证输出的是最小冲突链；
- 来源链：每条约束记录从根跳 `<manifest>` 到提出者的完整路径
  （`谁@版本 -> 谁@版本 -> requires 包 范围`）。

## 测试

```bash
pip install -r requirements.txt
pytest -q
```

测试覆盖 SemVer 解析（含预发布排序与 npm 范围语义）、求解器
（可行/无解/深层传递/多冲突枚举/预算保护）、全部 HTTP 接口（临时 SQLite，互不污染）、
批量审计（混合结果逐项隔离与计数、空/超限 422、求解与持久化异常隔离、成功项分别落库）、
异步作业（单条/裸数组/包装体提交与 202、按序执行、混合结果逐项隔离与计数、
单项/整单取消与 404/409、求解异常隔离、重启后 running→failed(interrupted) 且 queued 续跑、
bootstrap 幂等）、
历史影响反查（resolution/冲突约束/来源链命中、层级与稳定排序、版本聚合、未知包空结果），
以及导出包的确定性、重复/未知 ID 错误码与逐字节/顺序篡改识别。

## 项目结构

```
app/
  main.py       # FastAPI 路由与生命周期（含后台作业执行器启停）
  jobs.py       # 异步作业后台执行器：守护线程按序消费、作业视图组装
  processing.py # 单条清单校验+求解共享流水线（同步接口/批量/作业共用，异常归一）
  bootstrap.py  # 启动初始化：建表、播种、重启恢复（python -m app.bootstrap）
  solver.py     # 回溯求解器、冲突极小化与来源链
  semver.py     # SemVer 2.0.0 与版本范围解析
  database.py   # SQLite 建表/注册表/审计记录/异步作业（audit_jobs、audit_job_items）
  models.py     # Pydantic 请求/响应模型
  impact.py     # 历史影响反查：resolution/sources/conflicts 扫描与层级计算
  bundle.py     # 可验证 JSONL 导出包的确定性构造与校验（sha256/gzip，纯内存）
data/
  seed_registry.json   # 示例注册表
tests/                  # pytest 测试套件
Dockerfile
docker-compose.yml
entrypoint.sh           # 容器入口：先 app.bootstrap 初始化/恢复，再启动 uvicorn
```

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AUDIT_DB_PATH` | 容器内 `/data/audits.db`，本地 `./data/audits.db` | SQLite 数据库路径 |
| `AUDIT_SEED_PATH` | 容器内 `/app/data/seed_registry.json` | 首次启动的示例注册表 |
