# Agent Platform（v1）

能力分类、准入理由和完整兼容矩阵以
[ADR-0001](decisions/ADR-0001-agent-platform-boundary.md) 为准。

Agent Platform 是 Core 的外挂式 Agent 服务，不是数据服务。调用方向固定为：

```text
Core Service --Kitex--> Agent Platform (Go + Eino ReAct Agent) --> allowlisted retrieval tool --private HTTP--> Python Core data Gateway
```

## Gateway 分层

- 外部调用方继续调用既有 Core HTTP Gateway；现有 CLI、Knowledge HTTP、Retrieval HTTP 不经过
  Agent Platform。
- Core 使用 Kitex 调用 Agent Platform 的 `Invoke` 请求 `cap.api_doc.agent.v1`；已有业务仍可调用
  `ExecuteKnowledgeAssist`。Client 必须启用
  `client.WithTransportProtocol(transport.TTHeader)`，并通过 Kitex metainfo
  `x-core-service-key` 携带网关服务凭证；默认传输协议不会传递该 metainfo。
- Agent Platform 的 Eino Agent 只允许调用 `tool.api_doc.retrieve.v1` 一次。该工具仅使用
  `X-Agent-Platform-Key` 调用 Core 的私有数据面：
  `POST /internal/v1/agent-data/retrieval/context`。
- 私有数据面复用 Python `RetrievalPipelineService`，固定 `update_wiki=false`；Wiki 查询、原始
  RAG fallback、MongoDB、Zvec 和来源 Adapter 均停留在 Python Core。Agent Platform 只持有
  OpenAI-compatible LLM 的运行时配置，用于 Eino 的答案合成，不接触 Python 的数据库或来源。

## v1 安全边界

Go 服务只持有 Core 私有数据面地址、服务密钥和可选的 LLM provider 配置；不访问 MongoDB、
Zvec、Redis 或来源站点。Agent 在每次回答前必须先调用受控检索工具，工具参数中的
`wiki_id`、`namespace`、`version` 会由请求范围覆盖；没有可信结果就中止回答。

## 本地运行

1. Core `.env` 配置 `AGENT_PLATFORM_API_KEY`。
2. Agent Platform 使用相同值配置 `AGENT_PLATFORM_CORE_DATA_KEY`，另外配置只用于
   Core → Agent Platform 的 `AGENT_PLATFORM_CORE_RPC_KEY`，并设置
   `AGENT_PLATFORM_CORE_DATA_URL=http://127.0.0.1:8000`。
3. Core Kitex client 配置 `client.WithTransportProtocol(transport.TTHeader)`，并在调用
   context 上用 `metainfo.WithValue(ctx, "x-core-service-key", key)` 设置与
   `AGENT_PLATFORM_CORE_RPC_KEY` 相同的值。
4. 若要启用 `cap.api_doc.agent.v1`，再配置 `AGENT_PLATFORM_LLM_API_KEY`、
   `AGENT_PLATFORM_LLM_BASE_URL` 和 `AGENT_PLATFORM_LLM_MODEL`；不配置时只禁用该 Agent
   的在线 Invoke，不会回退到 Handler 直连适配器。
5. 在 `agent-platform/` 执行 `go run .`。

当前 Kitex IDL 位于 `agent-platform/idl/agent_platform.thrift`。Core 的 Kitex client 是下一步
接入工作；本提交只提供 Agent Platform server、Python 私有数据 Gateway 与测试边界。
