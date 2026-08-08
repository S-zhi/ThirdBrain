# Agent Platform

Agent Platform 是独立的 Go + Eino 中间件。调用方向固定为：

```text
Core Service --Kitex--> Agent Platform --private HTTP--> Python Core data Gateway
```

Go 服务不连接 MongoDB、Zvec、Redis、LLM 或来源站点。v1 仅处理
`ExecuteKnowledgeAssist`：它将 Core 请求交给唯一允许的数据工具
`knowledge.retrieve_context.v1`，该工具调用 Python 的
`POST /internal/v1/agent-data/retrieval/context`。该 Python 数据面强制
`update_wiki=false`，由 Python 内部完成 Wiki → 原始 RAG fallback。

## 配置

```bash
export AGENT_PLATFORM_LISTEN_ADDR=:8890
export AGENT_PLATFORM_CORE_RPC_KEY=replace-with-core-rpc-key
export AGENT_PLATFORM_CORE_DATA_URL=http://127.0.0.1:8000
export AGENT_PLATFORM_CORE_DATA_KEY=replace-with-agent-platform-key
# Optional; defaults to 30000 and is shared by Kitex handler and Core HTTP client.
export AGENT_PLATFORM_TIMEOUT_MS=30000
go run .
```

Core 的 Kitex client 必须配置 `client.WithTransportProtocol(transport.TTHeader)`，并用
`metainfo.WithValue` 在每次调用中设置 `x-core-service-key`；仅调用 `WithValue` 而使用默认
传输协议不会把该凭证送到服务端。凭证值与 `AGENT_PLATFORM_CORE_RPC_KEY` 相同。Python Core 必须将
`AGENT_PLATFORM_API_KEY` 设置为 `AGENT_PLATFORM_CORE_DATA_KEY` 的相同值。这两组密钥用途独立。
生产环境应在私有网络中部署两端；
公网认证、多租户、SSO 和数据层 ACL 不属于 v1。

## 生成 RPC 代码

`idl/agent_platform.thrift` 是 Core → Agent Platform 的稳定 Kitex 契约。更新 IDL 后执行：

```bash
kitex -module github.com/S-zhi/ThirdBrain/agent-platform \
  -service thirdbrain.agent.platform idl/agent_platform.thrift
```

生成代码位于 `kitex_gen/`。Eino 的模型/Agent Runner 尚未接入：v1 先以单一、确定性、
只读的工具调用建立权限和通信边界，后续模型接入只能在该工具白名单内编排。

该能力的完整静态声明位于 `internal/capability/descriptor.go`。它只描述治理契约，不提供
动态注册、业务实现或数据访问。
