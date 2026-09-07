# Agent Platform

Agent Platform 是独立的 Go + Eino 中间件。调用方向固定为：

```text
Core Service --Kitex--> Agent Platform --Eino ReAct Agent--> allowlisted retrieval tool --private HTTP--> Python Core data Gateway
```

v1 对外暴露 `cap.api_doc.agent.v1`。Kitex `Invoke` 只做请求契约、范围和超时校验，
然后交给 Eino ReAct Agent。Agent 只允许调用一次
`tool.api_doc.retrieve.v1`，工具再调用 Python 的
`POST /internal/v1/agent-data/retrieval/context`。模型不得自行编造 Wiki、命名空间、版本或
API 事实；如果工具没有可信命中，Agent 会返回 `abstained`。

`ExecuteKnowledgeAssist` 仍保留给已有 Core 兼容调用；新的 API 文档服务走 `Invoke`。

## 配置

```bash
export AGENT_PLATFORM_LISTEN_ADDR=:8890
export AGENT_PLATFORM_CORE_RPC_KEY=replace-with-core-rpc-key
export AGENT_PLATFORM_CORE_DATA_URL=http://127.0.0.1:8000
export AGENT_PLATFORM_CORE_DATA_KEY=replace-with-agent-platform-key
# Optional; defaults to 30000 and is shared by Kitex handler and Core HTTP client.
export AGENT_PLATFORM_TIMEOUT_MS=30000
# Required to enable cap.api_doc.agent.v1 Invoke; any OpenAI-compatible endpoint is supported.
export AGENT_PLATFORM_LLM_API_KEY=replace-with-llm-key
export AGENT_PLATFORM_LLM_BASE_URL=https://api.openai.com/v1
export AGENT_PLATFORM_LLM_MODEL=gpt-4o-mini
go run .
```

如果不配置三个 `AGENT_PLATFORM_LLM_*` 变量，进程仍可以启动并提供能力发现，但 API 文档
Agent 的 `Invoke` 会返回 `CAPABILITY_UNAVAILABLE`，不会绕过 Agent 直接访问检索适配器。

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

生成代码位于 `kitex_gen/`。Eino Agent 的模型、工具白名单、一次调用限制、超时和结果校验
位于 `internal/agent/` 与 `internal/agenttool/`；Transport Handler 不直接持有检索适配器。

该能力的完整静态声明位于 `internal/capability/descriptor.go`。它只描述治理契约，不提供
动态注册、业务实现或数据访问。
