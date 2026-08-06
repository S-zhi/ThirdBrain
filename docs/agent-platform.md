# Agent Platform（v1）

能力分类、准入理由和完整兼容矩阵以
[ADR-0001](decisions/ADR-0001-agent-platform-boundary.md) 为准。

Agent Platform 是 Core 的外挂式 Agent 服务，不是数据服务。调用方向固定为：

```text
Core Service --Kitex--> Agent Platform (Go + Eino) --private HTTP--> Python Core data Gateway
```

## Gateway 分层

- 外部调用方继续调用既有 Core HTTP Gateway；现有 CLI、Knowledge HTTP、Retrieval HTTP 不经过
  Agent Platform。
- Core 使用 Kitex 调用 Agent Platform 的 `ExecuteKnowledgeAssist`。Client 必须启用
  `client.WithTransportProtocol(transport.TTHeader)`，并通过 Kitex metainfo
  `x-core-service-key` 携带网关服务凭证；默认传输协议不会传递该 metainfo。
- Agent Platform 仅使用 `X-Agent-Platform-Key` 调用 Core 的私有数据面：
  `POST /internal/v1/agent-data/retrieval/context`。
- 私有数据面复用 Python `RetrievalPipelineService`，固定 `update_wiki=false`；Wiki 查询、原始
  RAG fallback、MongoDB、Zvec、LLM、来源 Adapter 均停留在 Python Core。

## v1 安全边界

Go 服务只持有 Core 私有数据面地址和服务密钥；不配置或访问 MongoDB、Zvec、Redis、LLM 或
来源站点。Eino 当前只构建带受控 Core Context 的消息；未配置模型 Runner，因此不能增加工具、
触发写入或绕过 Python 的范围过滤。

## 本地运行

1. Core `.env` 配置 `AGENT_PLATFORM_API_KEY`。
2. Agent Platform 使用相同值配置 `AGENT_PLATFORM_CORE_DATA_KEY`，另外配置只用于
   Core → Agent Platform 的 `AGENT_PLATFORM_CORE_RPC_KEY`，并设置
   `AGENT_PLATFORM_CORE_DATA_URL=http://127.0.0.1:8000`。
3. Core Kitex client 配置 `client.WithTransportProtocol(transport.TTHeader)`，并在调用
   context 上用 `metainfo.WithValue(ctx, "x-core-service-key", key)` 设置与
   `AGENT_PLATFORM_CORE_RPC_KEY` 相同的值。
4. 在 `agent-platform/` 执行 `go run .`。

当前 Kitex IDL 位于 `agent-platform/idl/agent_platform.thrift`。Core 的 Kitex client 是下一步
接入工作；本提交只提供 Agent Platform server、Python 私有数据 Gateway 与测试边界。
