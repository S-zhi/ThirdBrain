# Knowledge Update Plane

本文定义上层 LLM Knowledge Wiki 的写入面。它与现有底层 API RAG 完全独立：文档由外部
导入组件直接提交；上层只把经过证据校验的派生知识写入独立的 Knowledge Wiki。Knowledge
写入面不读取、不调用原来的 API RAG。

## 边界

公开入口：

```python
update_knowledge(documents, options) -> UpdateResult
update_wiki(WikiUpdateInput, options) -> UpdateResult
```

应用 Gateway：

```text
POST /api/v1/knowledge/update
```

该接口只接收 Wiki 文档和更新选项，不接收原 API RAG 的 Collection 句柄。没有配置 LLM
Provider 时，接口返回脱敏的 `503 KNOWLEDGE_UPDATE_DISABLED`，只读查询仍可运行。

### 最小可工作请求体

```http
POST /api/v1/knowledge/update
Authorization: Bearer $KNOWLEDGE_API_KEY
Content-Type: application/json

{
  "wiki": {
    "wiki_id": "wiki:test",
    "rag_collections": [
      {
        "documents": [
          {
            "document_id": "doc-1",
            "wiki_id": "wiki:test",
            "namespace": "AscendC.910beta3",
            "version": "910beta3",
            "content_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "parts": [
              {
                "part_id": "part-1",
                "order": 0,
                "content": "DataMove must be called before Compute."
              }
            ]
          }
        ]
      }
    ]
  },
  "options": { "model": "fake-model" }
}
```

成功响应：

```json
{
  "operation_id": "op-test",
  "wiki_id": "wiki:test",
  "status": "completed",
  "documents_received": 1,
  "documents_created": 1,
  "documents_updated": 0,
  "documents_unchanged": 0,
  "documents_failed": 0,
  "validation": { "passed": true, "issues": [], "warnings": [] },
  "provenance_coverage": 1.0
}
```

显式失败态（接口不泄露 provider / 存储内部细节）：

| HTTP | 响应 `code` | 触发条件 |
|---|---|---|
| 401 | — | 未带或错带 `KNOWLEDGE_API_KEY`（路由依赖 `require_service_auth`） |
| 422 | `KNOWLEDGE_UPDATE_INVALID` | 领域校验未过：`part_id` 重复、`order` 未严格递增、`parent_part_id` 成环、`documents.wiki_id` 与 `wiki.wiki_id` 不一致等 |
| 503 | `KNOWLEDGE_UPDATE_DISABLED` | 未配置 `KNOWLEDGE_LLM_API_KEY` / `OPENAI_API_KEY` |
| 503 | `KNOWLEDGE_UPDATE_UNAVAILABLE` | Mongo / 索引 / 提取器运行期异常 |

完整字段约束以 FastAPI 自带 `/docs`（OpenAPI 3）为准；本节未列的隐式约束（如
`content_hash` 至少 16 字符、`namespace/version` 非空、至少一个 `part`）在
`src/knowledge/models.py` 的 pydantic 定义里。

它负责：原始 Source/Part 修订、LLM 结构化提取、证据校验、保守合并、staging、
发布和派生索引刷新。

它不负责：在线查询、回答生成、自动保存 exploration、用户偏好 memory，或修改
底层 RAG 的原始 API 文档。

一个 `WikiUpdateInput` 代表一个上层 Knowledge Wiki，它包含多个文档输入。文档可以带有
可选的 `SourceOrigin` 作为来源备注，但 Wiki 不要求来源系统是 RAG，也不会根据来源备注
访问外部系统。不同 Wiki 则在 Source、Artifact、catalog 和 Zvec 过滤字段上完全隔离。

> **推荐用法**：新的独立 LLM Wiki 写入可以把 `rag_collections` 当作一次请求的纯分组容
> 器使用，把每项的 `rag_collection_id` 留空（`RagCollectionInput` 允许空字符串）。
> 旧调用方（仍希望 Wiki 文档保留底层 collection 身份）则继续填写
> `rag_collection_id`，两种形态不会互相校验失败，但同一请求内请保持形态一致 —— 不要
> 一部分文档带 `rag_collection_id`、另一部分留空，否则后续 catalog 切换会让一部分文档
> 被识别为"无来源 collection"，降低可追溯性。

## 不变量

- `wiki_id` 是上层知识域的强制隔离边界；每次 `update_knowledge` 只能处理一个 Wiki。
- `SourceOrigin` 是可选 provenance 元数据；它不参与 Knowledge 查询或 Artifact 身份。
- `namespace` 与 `version` 原样存储，保留官方大小写；任何大小写归一化仅能用于
  未来读侧的候选匹配，不能用作实体身份或自动合并依据。
- 输入的 `parts` 是原始边界，必须保留 `part_id`、父子关系和顺序。二级检索 chunk
  是可重建派生物，不能替代 Source Part。
- 每个 Claim 至少有一个 `EvidenceRef`，且必须能在声明的 `document_id + part_id +
  content_hash` 中定位。
- 不同 namespace/version 的内容不能共享 Artifact 或写入关系图。
- LLM 只能建议 `create/update/keep_separate/needs_review`；只有规范身份完全一致时
  才允许自动更新既有 Artifact。
- Source 与 active Artifact 指针先进入 staging；校验通过后才发布。索引失败不会
  回滚正式知识，因为索引是可重建派生物。

## Mongo 可见性模型

`MongoKnowledgeRepository` 使用三类不可变记录：

- `knowledge_source_revisions`
- `knowledge_artifact_revisions`
- `knowledge_update_staging`

发布时先写不可达 revision，最后通过 `knowledge_catalog/_id=wiki_<hash>` 的单文档原子
更新切换该 Wiki 的 Source 与 Artifact 指针。读侧只以对应 Wiki catalog 指针为准，
因此不会看到半发布结果；不同 Wiki 的发布也不会互相制造乐观锁冲突。

## 缓存失效

Source 的 current state 记录 `content_hash` 和 compiler fingerprint。以下任何项变化
都会触发重新加工：

- 原始内容 hash
- extractor version
- prompt version
- model
- schema version

`compiler_fingerprint` 由 `UpdateOptions` 计算，公式（见
`src/knowledge/models.py:UpdateOptions.compiler_fingerprint`）：

```python
sha256(
  f"{extractor_version}\x1f{prompt_version}\x1f{model}\x1f{schema_version}"
)
```

默认值为 `extractor_version=v1`、`prompt_version=v1`、`model=model-v1`、
`schema_version=1`。四项任一变化都会让 fingerprint 变，触发对应 Source 重新加工。
`actor` 和 `force_reprocess` 不参与 fingerprint：`force_reprocess=true` 绕过缓存直接
重跑，`actor` 仅写入审计字段。

## 运行时适配器

组合根可以选择 `OpenAIKnowledgeExtractor` 作为 `KnowledgeExtractor`：它只请求 JSON
草稿，不持有发布权限；模型名、prompt version 与 extractor version 都会进入 compiler
fingerprint。`ZvecKnowledgeIndexWriter` 则把**已发布**的 Artifact Revision 写入独立的
`knowledge_wiki_v1` collection（dense + sparse）。它不会复用底层 API RAG collection，
也不会提供查询接口。

因此部署时的依赖方向固定为：

```text
Document intake ------> WikiUpdateInput -> OpenAIKnowledgeExtractor -> KnowledgeUpdateService
                                                               |-> MongoKnowledgeRepository
                                                               |-> ZvecKnowledgeIndexWriter
```

任意一个适配器都可以通过其 Protocol 端口替换；例如离线评测可注入本地 extractor 和
内存索引写入器。

## 模块二的唯一依赖

查询模块只能依赖 `src.knowledge.contracts.KnowledgeRepository` 的读方法和已发布的
模型：`ActiveArtifact`、`ArtifactRevision`、`EvidenceRef`。它不得调用
`KnowledgeUpdateService` 或对 Knowledge Wiki 产生写入。

## 鉴权

写入路由 `/api/v1/knowledge/update` 沿用 `require_service_auth`（见
`src/gateway/auth.py`），环境变量 `KNOWLEDGE_API_KEY` 未配置时接口默认 503 关闭
（`Service authentication is not configured`），应用仍可正常启动。

调用方任选其一传 `KNOWLEDGE_API_KEY` 的明文值：

```http
Authorization: Bearer <KNOWLEDGE_API_KEY>
```

```http
X-API-Key: <KNOWLEDGE_API_KEY>
```

`Authorization` 必须是 `Bearer ` 前缀（区分大小写）；`X-API-Key` 直接传明文。两路都
使用 `secrets.compare_digest` 常量时间比较，避免计时泄露。401 响应附带
`WWW-Authenticate: Bearer` 头。

> **不要**把 `KNOWLEDGE_API_KEY` 复用为 LLM provider key —— 路由的鉴权 key 与
> `KNOWLEDGE_LLM_API_KEY` / `OPENAI_API_KEY` 完全独立；前者只控"谁可以调写入路由"，
> 后者控"能不能跑 LLM 提取"。
