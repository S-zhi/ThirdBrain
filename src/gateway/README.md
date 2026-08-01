# Agent API 文档查询 Gateway

Gateway 使用 FastAPI 暴露异步 HTTP 接口，负责请求校验、OpenAPI Schema、请求标识生成、
Service 命令转换和 HTTP 响应转换。API 文档检索逻辑位于 `src/service`，Gateway 不直接访问
索引、向量数据库或文档存储。

## 当前状态

查询链路已经接入：`name` 执行严格名称/API ID 查询，`semantic` 仅执行 dense 语义
召回。每次查询都会 best-effort 保存 MongoDB 结果快照。请求格式不符合 Schema 时返回
HTTP `422`，检索失败时单次接口返回 HTTP `503`。

## 启动服务

在项目根目录执行：

```bash
uv run uvicorn src.main:app --reload
```

默认地址：

- API Base URL：`http://127.0.0.1:8000`
- Swagger UI：<http://127.0.0.1:8000/docs>
- OpenAPI JSON：<http://127.0.0.1:8000/openapi.json>

## 接口概览

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/api/v1/agent/query/once` | 执行一次 API 文档查询 |
| `POST` | `/api/v1/agent/query/batch` | 批量执行 API 文档查询，最多 100 项 |
| `POST` | `/api/v1/admin/rag-construction/markdown/extract` | 通过受信任来源 Adapter 提取单页 Markdown |
| `POST` | `/api/v1/admin/rag-construction/yaml/convert` | 将 Markdown 转为指定 RAG Profile 的 YAML |
| `POST` | `/api/v1/admin/rag-construction/zvec/index` | 校验 YAML 并写入指定 Zvec store |
| `POST` | `/api/v1/admin/rag-construction/pipeline/run` | 执行提取、转换、入库的完整流程 |

两种接口都支持以下查询方式：

- `name`：根据函数名或完整 API ID 精确查询，例如 `DataStoreBarrier`、
  `com.huawei.cann.ascendc.op.910beta3.datastorebarrier`。
- `semantic`：根据自然语言描述查询，例如“把字符串转换成时间”。

## 请求字段

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `query` | `string` | 是 | 查询内容；自动去除首尾空格，处理后不能为空 |
| `query_type` | `name \| semantic` | 是 | 查询方式 |
| `top_k` | `integer` | 否 | 返回数量，默认 `5`，范围 `1`～`20` |
| `filters.namespace` | `string` | 是 | 完整命名空间 |
| `filters.version` | `string` | 是 | 精确版本 |
| `filters.language` | `string \| null` | 否 | 按语言过滤 |

所有请求模型都禁止未知字段。非空字符串字段会先去除首尾空格再校验。

## 单次查询

### 请求示例

```bash
curl -i -X POST 'http://127.0.0.1:8000/api/v1/agent/query/once' \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "DataStoreBarrier",
    "query_type": "name",
    "top_k": 5,
    "filters": {
      "namespace": "com.huawei.cann.ascendc.op.910beta3",
      "version": "910beta3",
      "language": "cpp"
    }
  }'
```

自然语言查询只需将 `query_type` 改为 `semantic`：

```json
{
  "query": "在核函数中执行数据同步",
  "query_type": "semantic",
  "top_k": 5,
  "filters": {
    "namespace": "com.huawei.cann.ascendc.op.910beta3",
    "version": "910beta3",
    "language": "cpp"
  }
}
```

### 成功响应模型

成功响应为 HTTP `200`：

```json
{
  "request_id": "6cb5e534-e3dc-4390-a318-0ff171478227",
  "query_record_id": "b6221994-f260-477d-a999-8dc5b05598db",
  "record_status": "recorded",
  "query": "DataStoreBarrier",
  "query_type": "name",
  "documents": [
    {
      "api_id": "com.huawei.cann.ascendc.op.910beta3.datastorebarrier",
      "name": "DataStoreBarrier",
      "api_name": "数据同步接口",
      "namespace": "com.huawei.cann.ascendc.op.910beta3",
      "version": "910beta3",
      "kind": "function",
      "language": "cpp",
      "version_support": ["910B"],
      "deprecated": false,
      "ingested_at": 1785160800,
      "signature": "void DataStoreBarrier()",
      "description": "执行数据同步。",
      "parameters_md": "",
      "returns_json": "",
      "examples": [],
      "source_markdown": "完整 API 文档内容",
      "deprecation_note": "",
      "score": null
    }
  ],
  "total": 1
}
```

名称查询的 `score` 为 `null`；语义查询返回 Zvec dense score。

## 批量查询

批量请求的 `items` 至少包含 1 项、最多包含 100 项。每项必须提供非空且在当前批次中
唯一的 `custom_id`，并允许混合使用不同的 `query_type`。

### 请求示例

```bash
curl -i -X POST 'http://127.0.0.1:8000/api/v1/agent/query/batch' \
  -H 'Content-Type: application/json' \
  -d '{
    "items": [
      {
        "custom_id": "query-1",
        "query": "DataStoreBarrier",
        "query_type": "name",
        "top_k": 5,
        "filters": {"namespace": "com.huawei.cann.ascendc.op.910beta3", "version": "910beta3"}
      },
      {
        "custom_id": "query-2",
        "query": "在核函数中执行数据同步",
        "query_type": "semantic",
        "top_k": 5,
        "filters": {"namespace": "com.huawei.cann.ascendc.op.910beta3", "version": "910beta3"}
      }
    ]
  }'
```

### 成功与部分失败响应模型

批量请求整体返回 HTTP `200`。单项失败只写入对应结果的 `error`，
不影响其他项：

```json
{
  "request_id": "848ff5a7-e77b-47e8-aed6-048d3218cb80",
  "batch_id": "eef9a1d9-d9e8-4acd-9a11-b64c521e66e5",
  "results": [
    {
      "custom_id": "query-1",
      "query_record_id": "3b655435-2e11-4622-bbd7-f2e18d661ebb",
      "record_status": "recorded",
      "query": "DataStoreBarrier",
      "query_type": "name",
      "documents": [],
      "total": 0,
      "error": null
    },
    {
      "custom_id": "query-2",
      "query_record_id": "21a148ca-6d5b-4f86-84c7-d9098644228e",
      "record_status": "recorded",
      "query": "在核函数中执行数据同步",
      "query_type": "semantic",
      "documents": [],
      "total": 0,
      "error": {
        "code": "RETRIEVAL_FAILED",
        "message": "查询失败",
        "request_id": "848ff5a7-e77b-47e8-aed6-048d3218cb80",
        "query_record_id": "21a148ca-6d5b-4f86-84c7-d9098644228e",
        "record_status": "recorded"
      }
    }
  ]
}
```

## Python 调用示例

客户端安装了 `httpx` 时，可以这样调用：

```python
import httpx


response = httpx.post(
    "http://127.0.0.1:8000/api/v1/agent/query/once",
    json={
        "query": "DataStoreBarrier",
        "query_type": "name",
        "top_k": 5,
        "filters": {
            "namespace": "com.huawei.cann.ascendc.op.910beta3",
            "version": "910beta3",
        },
    },
    timeout=10.0,
)

print(response.status_code)
print(response.json())
```

检索成功返回 `200`；查询记录写入失败不会阻断结果，响应中的 `record_status` 为 `failed`。

## RAG 构建接口

构建接口与查询接口分离：前者负责受控地把来源页面变成可检索的 Zvec 文档，后者只负责
召回。四个接口调用同一个 `RagConstructionService`，因此单独调用和完整流程始终使用相同的
Markdown Parser、YAML Mapper、Schema Profile 和 Zvec 映射。

`/markdown/extract` 的 `source_id` 必须来自 `configs/document_sync.yaml` 中已启用的来源；URL
仍会经过该 Adapter 的 allowlist、重定向和 robots 策略校验。接口不接受本地文件路径或任意
collection 名。

### 分阶段调用

Markdown 提取：

```json
{
  "source": {
    "source_id": "hiascend-cann-910beta3",
    "url": "https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/910beta3/API/ascendcopapi/example.html"
  }
}
```

Markdown 转 YAML：

```json
{
  "profile_id": "api-document-zvec/v2.1",
  "markdown": "# CreateTensor\n\n创建张量。",
  "source_name": "create_tensor.md",
  "source_url": "https://docs.example.com/create_tensor",
  "hints": {
    "namespace": "com.example.api",
    "version": "v1",
    "language": "cpp"
  }
}
```

YAML 入库：

```json
{
  "profile_id": "api-document-zvec/v2.1",
  "store_alias": "schema21",
  "yaml_content": "schema_version: '2.1'\ndocuments: []\n",
  "source_name": "create_tensor.yaml",
  "dry_run": false
}
```

`dry_run: true` 会执行 YAML Schema、Profile 映射和目标表结构校验，但不会创建 Embedder 或
写入 Zvec。一个批次中出现重复 `chunk_id` 时，会跳过该 ID 的全部记录，避免 upsert 顺序决定
最终内容。

### 完整流程

```json
{
  "source": {
    "source_id": "hiascend-cann-910beta3",
    "url": "https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/910beta3/API/ascendcopapi/example.html"
  },
  "profile_id": "api-document-zvec/v2.1",
  "store_alias": "schema21",
  "hints": {
    "namespace": "com.example.api",
    "version": "v1"
  },
  "options": {
    "dry_run": false,
    "include_intermediate_artifacts": true
  }
}
```

完整流程响应带有 `run_id` 与每个阶段的 `duration_ms`。若某阶段失败，错误响应会给出
`failed_stage` 和 `completed_stages`，但不会泄露 Adapter、模型或向量后端的原始异常。

### 动态选择向量库

请求只传 `store_alias`。默认别名为：

- `default`：`config.yaml` 中的 `zvec.default_collection`。
- `schema21`：`config.yaml` 中的 `zvec.shadow_collection`。

部署时可用独立环境变量扩展或覆盖别名，不需要变更全局 `ZvecConfig`：

```ini
RAG_CONSTRUCTION_ZVEC_STORES={"staging":"api_docs_staging","benchmark":"api_docs_benchmark"}
```

同一个 `profile_id` 每次请求都会按别名重新绑定 collection，所以同一 Schema 可用于生产、灰度
和 Benchmark 等不同物理库。需要替换来源清单时设置：

```ini
RAG_CONSTRUCTION_DOCUMENT_SYNC_CONFIG=configs/document_sync.yaml
```

构建接口会自动注册为 FastAPI 应用的生命周期依赖；它与已有
`/api/v1/admin/yaml-imports/batch` 不同，后者写 MongoDB 原始 YAML，前者写 Zvec 检索数据。

## Gateway 实现说明

一次请求的处理流程如下：

```text
HTTP 请求
  → Pydantic 参数校验与字符串规范化
  → Gateway Schema 转换为 Service dataclass command
  → AgentQueryService.query_once/query_batch
  → Service result 转换为 Gateway response
  → HTTP 200
```

Service 的同步 Zvec/Embedding 调用在线程中执行；检索失败由 Gateway 转换为带
`request_id/query_record_id/record_status` 的 HTTP `503`。其他传输入口仍可复用
`src/service` 中的 command、result 和 Service。

相关实现：

- `src/gateway/schemas.py`：HTTP 请求、响应及校验模型。
- `src/gateway/router.py`：HTTP 路由、Service 分发和响应转换。
- `src/gateway/__init__.py`：Gateway router 导出。
- `src/service/agent_query_service.py`：传输无关的 Service 契约。
- `src/main.py`：FastAPI 应用与路由注册。

## YAML 批量导入 MongoDB

内部管理接口 `POST /api/v1/admin/yaml-imports/batch` 将提取脚本生成的 YAML 文件写入
调用方指定的 MongoDB Collection。

写入约定：

- 一个 YAML 文件对应一条 MongoDB 记录，除稳定 `_id` 外不改变字段层级。
- 同时兼容扁平 Schema 1.0 和包含 `documents` 的 Schema 2.0；Schema 2.0 不拆分记录。
- 批次允许 1～100 项，单项失败不影响其他文件。
- Schema 1.0 使用顶层 `chunk_id`、Schema 2.0 使用 `source.content_hash` 判断重复；重复导入
  不新增记录，单项状态返回 `duplicate`，`inserted_id` 返回已有记录 ID。
- Collection 名必须匹配 `^[a-z][a-z0-9_]{0,62}$`。

允许读取的目录与单文件大小通过环境变量配置：

```ini
RAG_YAML_IMPORT_ALLOWED_ROOTS=["./yaml","./ingest/output"]
RAG_YAML_IMPORT_MAX_FILE_BYTES=10485760
```

请求示例：

```json
{
  "items": [
    {
      "custom_id": "ai-cpu-001",
      "file_path": "/absolute/path/to/yaml/AI_CPU_API/DataStoreBarrier.yaml",
      "collection": "api_docs_ai_cpu"
    }
  ]
}
```

成功或重复结果包含 `inserted_id` 和识别到的 `schema_version`，汇总中的
`duplicate_count` 表示命中已有记录的数量。文件不存在、路径越界、YAML
错误、Schema 错误或 MongoDB 写入错误放在对应 `results[].error` 中，合法批次仍返回 HTTP
200。该接口可以读取 Server 文件并选择 Collection，必须部署在内部网络并由上游网关或
鉴权中间件保护。
