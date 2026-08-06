# ADR-0001：Agent Platform 能力分类、v1 白名单与服务边界

- 状态：Accepted
- 日期：2026-08-06
- 决策方：Third Brain / Agent Platform
- 后续任务：任务 2–10 必须引用本 ADR，不再使用“统一 Agent Runtime”或“整个项目零数据库”作为前提。

## 1. 决策摘要

Agent Platform 是独立 Go 服务，是 Core 的 Agent 交互外挂，不是数据服务，也不是把 Third
Brain 全部能力重写成单一 Runtime：

```text
现有调用方 ──CLI/HTTP──> Python Core（原路径保持）
Core ──Kitex──> Agent Platform（Go + Eino）
Agent Platform ──private HTTP──> Python Core data Gateway ──> Hub/MongoDB/Zvec/既有 Python 服务
```

Go 只负责 Agent 编排、能力准入、deadline/cancel、调用链 ID、错误归一和脱敏日志。数据获取、
检索、摄取、Wiki 编译、持久化和 provider 调用仍由原 Python 项目负责。Agent Platform 不复制
业务逻辑、不新增数据库，不直接连接 Hub、MongoDB、Zvec、Redis、LLM 或来源站点。

## 2. 五类能力决策

| 稳定 ID | 能力分类 | Python 所属模块 | 调用模式 | 风险 | 主要依赖 | 契约责任方 | 版本策略 | v1 决策与理由 |
|---|---|---|---|---|---|---|---|---|
| `api_docs.loop_lookup.v1` | API 文档检索服务；面向 LLM Wiki 的信息 Loop 查找 | Knowledge 查询/文档检索 Gateway | 在线、同步、低频 | 只读、低影响 | Knowledge Reader、Hub/文档源适配器 | 输入：Core；输出：Python API 文档域 | 兼容加字段；破坏性变更新增 `.v2` | 延期。接口应保留，但当前真实调用由统一确定性检索覆盖；待 Loop 查询契约稳定后接入，避免 Go 重复检索逻辑。 |
| `documents.structure_ingest.v1` | 文档摄取与结构化处理 | `ingest/`、文档同步和构建服务 | 离线、长耗时 | 写入、高影响 | 白名单源、解析器、MongoDB/Zvec | 输入/输出：Python 摄取域 | 作业输入 schema 与产物 schema 分别版本化 | 延期。不得从在线 Agent 路径触发；后续只能进入受控离线操作面，必须有幂等键、dry-run 和审计。 |
| `knowledge.wiki_compile.v1` | Knowledge Wiki 编译器 | `src/knowledge/` 编译/更新链路 | 离线、长耗时 | 写入、高影响 | MongoDB、Zvec、LLM/provider、关系图 | 输入/输出：Python Knowledge 域 | 编译器版本与产物 schema 双版本 | 延期。当前缺少适合作业调用的稳定幂等契约；Go 不实现编译逻辑。 |
| `knowledge.retrieve_context.v1` | Knowledge + 原始 RAG 确定性检索编排 | `src.retrieve.pipeline.RetrievalPipelineService` | 在线、同步 | 只读、低影响 | Knowledge Reader、原始 RAG fallback、MongoDB/Zvec（均由 Python 持有） | 输入：Kitex IDL；输出：Python `QueryKnowledgeResult` | v1 只兼容加可选字段；破坏性变更新 Kitex 方法或 `.v2` | **纳入 v1 唯一在线白名单**。路径确定、只读，并强制 `update_wiki=false`。 |
| `pipeline.skill_run.v1` / `benchmark.run.v1` | Pipeline Skill / Benchmark | `pipeline`/`benchmark/` 与既有 runner | 离线、长耗时 | 高计算；可写报告/产物 | cases、runner、模型/provider、结果存储 | 输入/输出：对应 Python runner | case schema、runner、模型和数据集均显式版本化 | 不纳入在线 v1，延期到离线操作面。结果必须可复现；在线 Agent 无权触发。 |

“延期”不是删除能力，而是本次 Agent Platform 不暴露对应可调用入口。只有
`knowledge.retrieve_context.v1` 可以从在线 Kitex 请求进入。

## 3. v1 准入白名单

这里的“白名单”指 Agent Platform 当前允许编排和调用的能力集合，不是 Third Brain 的全部功能
清单，也不是要求 CLI/HTTP 改走 Agent Platform。

| 能力 ID | 状态 | 工具调用上限 | 超时 | 权限 |
|---|---|---:|---:|---|
| `knowledge.retrieve_context.v1` | Enabled | 每次请求 1 次 | 默认 10 秒，并服从上游 deadline/cancel | `core.private.retrieval.read` |

未列出的网络、文件、数据库、密钥、摄取、编译、Benchmark 和任意工具调用默认拒绝。当前代码以
静态 descriptor 声明能力，不实现动态注册表。

## 4. 入口兼容矩阵

| 现有入口/调用方 | 继续访问 | 是否经过 Agent Platform | Agent Platform 责任 |
|---|---|---:|---|
| 维护者 CLI | 原 Python CLI/服务 | 否 | 无；不解析、不代理、不改变输出 |
| Knowledge HTTP | 原 Python Knowledge Gateway | 否 | 无 |
| Retrieval HTTP | 原 Python Retrieval Gateway | 否 | 无 |
| Python 内部调用 | 原 service/repository 调用 | 否 | 无 |
| Core 的 Agent 交互 | Kitex `ExecuteKnowledgeAssist` | 是 | 鉴权、策略约束、Eino 编排、错误/trace 归一 |
| Agent Platform 数据查询 | Python `/internal/v1/agent-data/retrieval/context` | 从 Go 发起 | 只调用已登记私有数据契约，不解释或复制检索业务 |

因此，“统一能力调用层”只统一 Agent 能力调用，不统一展示层，也不接管现有 CLI 和公开 HTTP。

## 5. Gateway 权限边界与依赖归属

| 边界 | v1 鉴权 | 责任方 |
|---|---|---|
| 外部/CLI → Core HTTP | 沿用 Core 现有鉴权 | Python Core Gateway |
| Core → Agent Platform Kitex | TTHeader + Kitex metainfo `x-core-service-key` | Agent Platform Gateway middleware |
| Agent Platform → Core 私有 HTTP | `X-Agent-Platform-Key` | Python Core 私有数据 Gateway |
| Core → Hub/MongoDB/Zvec/Redis/provider | 沿用原项目连接与配置 | Python Core；v1 暂不重新设计底层服务权限 |

两段服务密钥必须独立。request ID、correlation ID 和 deadline/cancel 从 Kitex 调用向 Python 数据
请求传播；密钥、Authorization 和敏感配置不得进入 trace 或业务错误响应。

Core Kitex client 必须显式启用 `client.WithTransportProtocol(transport.TTHeader)`；否则
`metainfo.WithValue` 的服务凭证不会通过默认传输协议到达 Agent Platform，调用将被拒绝。

## 6. 明确不承诺

- 不提供公网 Agent API、多租户、SSO 或用户级 RBAC。
- 不新增 Agent Platform 独立数据库；“项目零数据库”不成立，MongoDB/Zvec 等仍由 Core 使用。
- 不把 Python 业务逻辑移植到 Go，不让 Go 直接获取业务数据。
- 不实现动态 SDK/注册表/通用适配器，也不允许未登记工具。
- Eino v1 仅建立受控编排边界；模型 Runner 未接入前，执行保持单工具、确定性、只读。

## 7. 变更规则

新增在线能力必须先更新本 ADR 或后续 ADR、静态 descriptor、权限和测试，再进入白名单。写入、
高影响或离线能力不得仅通过增加一个 Eino Tool 就进入在线请求路径。
