# 统一检索编排

`RetrievalPipelineService` 将两个已有能力面串成一条可观测链路：

```text
query → LLM Wiki → hit 直接返回
                   miss/弱命中 → exact + dense + sparse → RRF
                   → Context Capsule → 返回 → Wiki 更新调度
```

统一入口是 `POST /api/v1/retrieval/query`。请求始终携带 `wiki_id`、
`namespace` 和 `version`，这些字段会同时约束 Wiki 与原始 RAG 的查询。

`RagSourceReader` 负责原始 API Collection 的精确、稠密和稀疏召回；
`KnowledgeUpdateServiceScheduler` 将 fallback 命中的原始文档转换成
`update_knowledge` 输入。没有配置 LLM provider 时，查询仍可返回 RAG 结果，
但响应会带 `LLM_WIKI_UPDATE_NOT_CONFIGURED` 告警和可重试的
`enrichment_requests`。
