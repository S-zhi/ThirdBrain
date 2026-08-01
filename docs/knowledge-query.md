# Knowledge Query（模块 2）

模块 2 是 Knowledge Wiki 的只读查询面。它负责把底层 API RAG 的原始文档命中、
模块 1 发布的派生知识和有限关系扩展融合成机器可消费的 Recall Capsule；它不负责生成、
发布或更新知识。

## 稳定入口

- 服务方法：`KnowledgeQueryService.query_knowledge(query, options)`
- HTTP：`POST /api/v1/knowledge/query`
- 必填范围：`namespace` + `version`
- namespace 按官方大小写精确匹配，不做大小写归一化

请求示例：

```json
{
  "query": "创建 stream 并设置优先级",
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

模块 2 通过 `KnowledgeReader` 协议读取数据。当前生产组装仅接入现有 Zvec Source RAG，
并行复用其 exact 和 dense 查询；不改写 Zvec 的索引、过滤规则或召回实现。

所有 Reader 返回的候选会在编排层再次执行 namespace、version、language、生命周期和
provenance 硬校验。派生知识没有来源证据时不会进入结果。

## 与模块 1 的对接

模块 1 合并后只需提供两个只读适配器，不需要修改查询算法：

1. `KnowledgeReader.search(...)`：读取已发布的派生知识，返回按各通道排序的
   `ReaderSearchResult`。
2. `RelationReader.expand(...)`：按种子 ID 和版本范围做有界的一跳关系扩展。

然后在 `build_knowledge_query_service` 中把当前的 `EmptyKnowledgeReader` 和
`EmptyRelationReader` 替换为模块 1 的实现。Reader 异常时查询面按通道降级；Source 与
Artifact Reader 同时不可用时才返回 `503 KNOWLEDGE_READER_UNAVAILABLE`。

## 排序和预算

- 多通道融合使用确定性的 RRF（`k=60`）。
- exact、alias、metadata、置信度和 active 状态使用显式小幅加成。
- 图扩展只影响有界候选，不能绕过版本和命名空间过滤。
- 排名分数相同时按类型、namespace、version、标题和 ID 稳定排序。
- Capsule 同时受条目数、单条字符数和整包字符数限制，并返回粗略 token 估算。

## 当前限制

在模块 1 尚未接入时，生产服务只返回 Source 命中，并把对应文档列入
`enrichment_requests`。这是显式的冷启动状态，不会伪造派生知识，也不会在查询请求中
隐式调用 LLM 或写入知识库。
