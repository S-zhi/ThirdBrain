# Knowledge Update Plane

本文定义上层 LLM Knowledge Wiki 的写入面。它与现有底层 API RAG 分层：底层
RAG 负责保留与召回原始 API 文档；上层只把经过证据校验的派生知识写入独立的
Knowledge Wiki。

## 边界

公开入口：

```python
update_knowledge(documents, options) -> UpdateResult
update_wiki(WikiUpdateInput, options) -> UpdateResult
```

它负责：原始 Source/Part 修订、LLM 结构化提取、证据校验、保守合并、staging、
发布和派生索引刷新。

它不负责：在线查询、回答生成、自动保存 exploration、用户偏好 memory，或修改
底层 RAG 的原始 API 文档。

一个 `WikiUpdateInput` 代表一个上层 Knowledge Wiki，它包含多个 `RagCollectionInput`。
每个底层 RAG Collection 都有精确的 `rag_collection_id`；每份 Source 同时带有
`wiki_id + rag_collection_id`。同一 Wiki 内不同 Collection 的同名知识可在严格身份
一致时合并；不同 Wiki 则在 Source、Artifact、catalog 和 Zvec 过滤字段上完全隔离。

## 不变量

- `wiki_id` 是上层知识域的强制隔离边界；每次 `update_knowledge` 只能处理一个 Wiki。
- `rag_collection_id` 是 Source provenance 的强制字段；同一 document_id 可以出现在
  不同 Collection，不能因此覆盖或混淆来源。
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

## 运行时适配器

组合根可以选择 `OpenAIKnowledgeExtractor` 作为 `KnowledgeExtractor`：它只请求 JSON
草稿，不持有发布权限；模型名、prompt version 与 extractor version 都会进入 compiler
fingerprint。`ZvecKnowledgeIndexWriter` 则把**已发布**的 Artifact Revision 写入独立的
`knowledge_wiki_v1` collection（dense + sparse）。它不会复用底层 API RAG collection，
也不会提供查询接口。

因此部署时的依赖方向固定为：

```text
RAG Collection A --\
RAG Collection B ----> WikiUpdateInput -> OpenAIKnowledgeExtractor -> KnowledgeUpdateService
RAG Collection N --/                                      |-> MongoKnowledgeRepository
                                                          |-> ZvecKnowledgeIndexWriter
```

任意一个适配器都可以通过其 Protocol 端口替换；例如离线评测可注入本地 extractor 和
内存索引写入器。

## 模块二的唯一依赖

查询模块只能依赖 `src.knowledge.contracts.KnowledgeRepository` 的读方法和已发布的
模型：`ActiveArtifact`、`ArtifactRevision`、`EvidenceRef`。它不得调用
`KnowledgeUpdateService` 或对 Knowledge Wiki 产生写入。
