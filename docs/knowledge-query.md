# Knowledge Query（模块 2）

模块 2 是 Knowledge Wiki 的只读查询面。它负责把底层 API RAG 的原始文档命中、
模块 1 发布的派生知识和有限关系扩展融合成机器可消费的 Recall Capsule；它不负责生成、
发布或更新知识。

## 稳定入口

- 服务方法：`KnowledgeQueryService.query_knowledge(query, options)`
- HTTP：`POST /api/v1/knowledge/query`
- 必填范围：`wiki_id` + `rag_collection_ids` + `namespace` + `version`
- namespace 按官方大小写精确匹配，不做大小写归一化
- 服务默认安全关闭；必须配置 `KNOWLEDGE_API_KEY`，调用方使用 Bearer 或 `X-API-Key`

请求示例：

```json
{
  "query": "创建 stream 并设置优先级",
  "wiki_id": "wiki-ascendc",
  "rag_collection_ids": ["ascendc-official-v8"],
  "namespace": "AscendC.runtime",
  "version": "v8.0",
  "language": "zh-CN",
  "top_k": 10,
  "budget": "medium",
  "include_stale": false,
  "expand_relations": true,
  "relation_limit": 6
}
```

响应包含：

- `recall_capsule`：按 micro/small/medium/large 预算裁剪的最小融合上下文
- `source_hits`：底层 RAG 原始文档命中
- `knowledge_hits`：概念、实体、对比卡、探索记录等派生知识命中
- `cache_misses` / `enrichment_requests`：只读地声明缺失，不触发写入
- `follow_up`：供上层 Agent 决定是否调用 `update_knowledge`
- `trace`：`trigger → recall → rerank → inject → generate` 五阶段记录；生成阶段归调用方

## 与底层 RAG 的边界

模块 2 通过 `KnowledgeReader` 协议读取数据。生产组装同时接入现有 Zvec Source RAG 与
模块 1 已发布 Artifact：Source Reader 复用 exact/dense 查询；Artifact Reader 以 MongoDB
active catalog 为事实边界，并把 Zvec 命中与已发布记录连接，未发布或过期的孤立向量不会返回。

所有 Reader 返回的候选会在编排层再次执行 wiki、RAG collection、namespace、version、
language、生命周期和 provenance 硬校验。派生知识没有来源证据时不会进入结果；弱单通道
向量命中会显式 abstain，不会伪装成可信答案。

## 与模块 1 的对接

模块 1 已通过只读适配器接入，不改变其 staging → validation → publish 写入流程：

1. `PublishedArtifactKnowledgeReader.search(...)` 从正式 active catalog 读取知识，并融合
   exact、alias、lexical 与 Zvec vector 通道。
2. `RelationReader.expand(...)` 保留为有界的一跳扩展接口；当前 artifact 自带关系会进入
   Capsule，但独立关系存储适配器仍未启用。

Reader 异常时查询面按通道降级；Source 与 Artifact Reader 同时不可用时才返回
`503 KNOWLEDGE_READER_UNAVAILABLE`。查询只报告 cache miss/enrichment request，不直接写库。

## 排序和预算

- 多通道融合使用确定性的 RRF（`k=60`）。
- exact、alias、metadata、置信度和 active 状态使用显式小幅加成。
- 图扩展只影响有界候选，不能绕过版本和命名空间过滤。
- 排名分数相同时按类型、namespace、version、标题和 ID 稳定排序。
- Capsule 同时受条目数、单条字符数和整包字符数限制，并返回粗略 token 估算。

## Link 兼容性边界

本模块参考 Link 的 source/concept/entity/comparison/exploration、provenance、confidence、
Recall Capsule 和读写分离语义，并适配为本项目的 wiki + RAG collection + namespace +
version 四层隔离。未迁移 Link 的前端、个人记忆、展示 CLI；关系图目前只保留模型与有界接口。
版权与许可证见仓库根目录 `THIRD_PARTY_NOTICES.md`。
