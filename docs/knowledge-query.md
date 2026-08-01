# Knowledge Query（模块 2）

模块 2 是 LLM Wiki 的只读查询面。它只读取模块 1 发布的 Knowledge Artifact 和独立的
Knowledge Zvec 索引，并将结果整理成机器可消费的 Recall Capsule；它不读取、不调用原来的
API RAG，也不负责生成、发布或更新知识。

## 稳定入口

- 服务方法：`KnowledgeQueryService.query_knowledge(query, options)`
- HTTP：`POST /api/v1/knowledge/query`
- 必填范围：`wiki_id` + `namespace` + `version`
- `rag_collection_ids` 仅作为可选来源标注，省略表示独立 LLM Wiki 查询
- namespace 按官方大小写精确匹配，不做大小写归一化
- 服务默认安全关闭；必须配置 `KNOWLEDGE_API_KEY`，调用方使用 Bearer 或 `X-API-Key`

请求示例：

```json
{
  "query": "创建 stream 并设置优先级",
  "wiki_id": "wiki-ascendc",
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

- `recall_capsule`：按 micro/small/medium/large 预算裁剪的最小 Knowledge 上下文
- `knowledge_hits`：概念、实体、对比卡、探索记录等已发布知识命中
- `source_hits`、`cache_misses`、`enrichment_requests`：为旧响应保留，但独立查询为空
- `follow_up`：供上层 Agent 决定是否扩大预算或执行其他独立任务
- `trace`：`trigger → recall → rerank → inject → generate` 五阶段记录；生成阶段归调用方

## 与底层 RAG 的边界

模块 2 通过 `KnowledgeReader` 协议只读取模块 1 已发布的 Artifact。Artifact Reader 以
MongoDB active catalog 为事实边界，并把 Knowledge Zvec 命中与已发布记录连接，未发布或
过期的孤立向量不会返回。

所有候选会在编排层再次执行 wiki、namespace、version、language、生命周期和 provenance
硬校验。派生知识没有来源证据时不会进入结果；弱单通道向量命中会显式 abstain，不会伪装
成可信答案。

## 与模块 1 的对接

模块 1 已通过只读适配器接入，不改变其 staging → validation → publish 写入流程：

1. `PublishedArtifactKnowledgeReader.search(...)` 从正式 active catalog 读取知识，并融合
   exact、alias、lexical 与 Zvec vector 通道。
2. `RelationReader.expand(...)` 保留为有界的一跳扩展接口；当前 artifact 自带关系会进入
   Capsule，但独立关系存储适配器仍未启用。

Artifact Reader 异常时返回 `503 KNOWLEDGE_READER_UNAVAILABLE`。查询不会退回原来的 API RAG，
也不会直接写库。

## 排序和预算

- 多通道融合使用确定性的 RRF（`k=60`）。
- exact、alias、metadata、置信度和 active 状态使用显式小幅加成。
- 图扩展只影响有界候选，不能绕过版本和命名空间过滤。
- 排名分数相同时按类型、namespace、version、标题和 ID 稳定排序。
- Capsule 同时受条目数、单条字符数和整包字符数限制，并返回粗略 token 估算。

## Link 兼容性边界

本模块参考 Link 的 source/concept/entity/comparison/exploration、provenance、confidence、
Recall Capsule 和读写分离语义，并适配为本项目的独立 wiki + namespace + version 三层隔离。
来源 Collection 只作为可选元数据。未迁移 Link 的前端、个人记忆、展示 CLI；关系图目前只
保留模型与有界接口。
版权与许可证见仓库根目录 `THIRD_PARTY_NOTICES.md`。
