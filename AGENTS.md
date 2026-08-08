# AGENTS.md

> **RAG With Cold API Documents** — 为代码 Agent 提供 API 知识检索与可信上下文服务，降低模型对陌生 / 版本敏感 API 的幻觉。

## 1. 项目定位

解决 Agent 的"不知道自己不知道"问题：Agent 生成或修改代码前，自动识别 API 使用意图，按**版本 + 命名空间**精确检索官方描述、参数契约与高相关示例，输出**机器可消费的上下文包**，而不是给人看的 HTML 文档。

### 来自 PRD 的 5 条核心原则

| # | 原则 | 应对的痛点 |
|---|---|---|
| 1 | **Version-first** — 任何召回必须带 `org.product.api.vN` 形式的版本/命名空间元数据 | 痛点 2：API 快速变化 → 旧知识污染 |
| 2 | **Namespace isolation** — 同名/相似 API 必须在不同命名空间/产品下分区存储与过滤 | 痛点 3：相似但错误的 API 命中 |
| 3 | **Machine-first 结构** — 文档带场景标签、参数契约、调用约束、反例 | 痛点 1：臆造 API；痛点 4：查到但用错 |
| 4 | **5 阶段 Trace** — `trigger → recall → rerank → inject → generate` 全链路可观测 | 痛点 5：效果无法解释 |
| 5 | **长期 Benchmark** — 可复现、可扩展、可回归的 API-RAG 评测 | 痛点 5；痒点「模型升级归因」 |

### 用户故事 → 模块映射

| Story | 体现模块 | 关键能力 |
|---|---|---|
| 陌生库意图识别 + 候选 + 参数契约 | `retrieve/` + `context/` | 意图→查询→粗排→精排→上下文包 |
| 专有 API（如 Sense）作为命名实体 | `ingest/` + `retrieve/` | `org.product.namespace.version` 四元组 + 命名实体识别 |
| 多候选接近分时生成对比卡 | `context/` | 候选差异卡：语义/参数/返回/版本/场景 |

## 2. 技术栈（待用户最终确认）

- **语言**：Python 3.11+（主，检索/摄取/评测），TypeScript（CLI/可视化）
- **Agent Platform**：Go（并发运行时与统一调用层）+ Eino（Agent / Workflow 编排）。Go 平台通过稳定的进程间或网络契约接入既有 Python 检索、摄取与评测能力；不要将 Agent SDK 耦合到某个 Python 内部实现。
- **服务**：FastAPI + pydantic v2
- **依赖管理**：`uv`
- **存储（当前实现）**：MongoDB（事实、发布状态与查询留痕）+ Zvec（进程内、可重建的派生索引）；Agent 中台不得新增独立数据库
- **LLM**：OpenAI / Anthropic / 本地 vLLM 抽象层，按 case 切换
- **质量**：ruff + mypy(strict) + pytest
- **可观测**：OpenTelemetry → Trace 五阶段结构化落库

> 用户拍板前先按默认推进。任何一项被否决，更新本节并同步 `docs/architecture.md`。

### Agent Platform 约束

- Agent 能力以静态声明注册，声明必须包含：能力 ID、分类、输入/输出 schema、版本、兼容范围、超时、权限边界与脱敏字段。
- v1 中 Core 仅可经 Kitex 请求 Agent Platform 的只读统一检索；Platform 再调用既有 Python 编排。摄取、Knowledge 编译、Pipeline 与 Benchmark 保持 Python 侧受控离线能力，不向 Core/Agent Platform 路由；禁止 Agent 直接访问任意文件、网络、密钥或未登记工具。
- 统一调用内核负责上下文传播、deadline/cancel、错误码归一、调用链 trace 与结构化脱敏日志。业务 Agent 不得各自实现重试、超时和日志脱敏策略。
- 本地配置、启动/停止/健康检查、能力发现和版本兼容校验属于平台生命周期；不满足兼容约束的能力不得被路由。
- Pipeline Skill 与 Benchmark 辅助能力为离线能力，不可由在线 Agent 请求路径直接调用；其结果须可复现并带版本标识。

## 3. 目录结构

```
.
├── core/            # 领域类型：API Document / Version / Namespace / Contract
├── ingest/          # 【独立 scripts，不属于运行时库】文档解析、结构化、版本对齐、增量同步、弃用状态
├── retrieve/        # 意图识别 → 查询构造 → 粗排 → 精排 → 重排
├── context/         # 上下文包构造：候选对比卡、参数契约、反例
├── trace/           # 5 阶段 Trace 数据模型、采集、查询接口
├── benchmark/       # API-RAG Benchmark：cases、runner、归因报告
├── service/         # FastAPI 入口（检索 / 摄取 / 评测 API）
├── cli/             # 维护者 CLI：摄取、跑评测、看 trace
├── agent-platform/  # Go + Eino：Capability SDK、注册发现、调用内核、生命周期与编排
├── tests/
│   ├── unit/        # 单测
│   ├── integration/ # 集成（需本地 MongoDB；部分场景需 Zvec / 外部 API）
│   └── benchmark/   # 评测 case（默认禁跑，CI 单独触发）
└── data/            # 离线语料、fixtures（不入仓）
```

## 4. 开发约定

- **分支**：`main` 为发布分支；功能从 `feat/<scope>-<short-desc>` 切出；hotfix 走 `fix/*`
- **提交**：Conventional Commits（`feat:` / `fix:` / `docs:` / `refactor:` / `test:` / `chore:`）
- **代码风格**：
  - Python: `ruff format` + `ruff check`，`line-length = 100`
  - 公共 API 强制 `mypy --strict`
  - 命名空间/版本字符串使用点分隔并保留官方大小写（如 `AscendC.910beta3`），禁止自行改写
  - 标识符英文；注释与文档中文
- **测试**：
  - 任何新行为必须有单测；新检索策略必须有 Benchmark case
  - 集成测试需使用本地 MongoDB fixture；涉及向量检索时使用 Zvec fixture 或临时 collection
  - 评测 case 走 JSON/YAML，schema 见 `docs/benchmark.md`
- **PR 模板**：必须勾选"对应 PRD 痛点"（P1–P5 之一），否则不 merge

## 5. 命令

| 操作 | 命令 |
|---|---|
| 安装 | `uv sync` |
| 开发 | `uv run uvicorn service.main:app --reload` |
| 单测 | `uv run pytest tests/unit` |
| 集成 | `uv run pytest tests/integration` |
| 评测 | `uv run pytest tests/benchmark --benchmark-only` |
| Lint | `uv run ruff check .` |
| Format | `uv run ruff format .` |
| Typecheck | `uv run mypy .` |

## 6. 待决项（Open Decisions）

- [x] **向量后端：Zvec** ✅（2026-07-27 选定；阿里开源 in-process 向量库，详见 `src/dao/emb/`）
- [ ] LLM 提供方：OpenAI / Anthropic / 本地 vLLM（建议先 OpenAI 跑通，再做抽象）
- [x] Embedding 提供方：**Bailian 千问 qwen3.7-text-embedding**（2048 维，主路径）；本地 sentence-transformers MiniLM（CI/离线 fallback）。`src/dao/emb/embedder.py`
- [ ] PRD 中"只查一次"策略的真实范围（PRD 标注待定）
- [ ] 文档结构 schema：pydantic 导出 JSON Schema / Protobuf
- [ ] Trace 落库：Postgres（同库）/ ClickHouse（独立 OLAP）
- [ ] 摄取源白名单首批：哪些公开 + 哪些组织内部
- [ ] 嵌入维度是否切到 FP16 节省存储（当前 FP32 基线）

## 7. 安全

- 永不提交密钥；`.env` 与 `.env.*` 已在 `.gitignore`
- 摄取源只走 `docs/ingest-sources.md` 白名单
- Benchmark 数据集若含私有 API，必须经 `cli/redact.py` 脱敏
- 任何对外暴露的 endpoint 默认走 service 鉴权中间件

## 8. 配套文档

- `docs/architecture.md` — 5 阶段 Trace 详细设计、数据流图
- `docs/data-model.md` — API Document / Context Package / Trace 字段定义
- `docs/benchmark.md` — 评测协议、归因报告模板、回归流程
- `docs/ingest-sources.md` — 文档源白名单与同步策略
- `docs/decisions/` — ADR 记录每个 Open Decision 的最终拍板
- `docs/decisions/ADR-0001-agent-platform-capability-boundary.md` — Agent 中台能力分类、v1 白名单与 Kitex/CLI/HTTP 边界

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **ThirdBrain** (31497 symbols, 53963 relationships, 300 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> Index stale? Run `node .gitnexus/run.cjs analyze` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? `npx gitnexus analyze` (npm 11 crash → `npm i -g gitnexus`; #1939).

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows. For regression review, compare against the default branch: `detect_changes({scope: "compare", base_ref: "main"})`.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `query({search_query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `context({name: "symbolName"})`.
- For security review, `explain({target: "fileOrSymbol"})` lists taint findings (source→sink flows; needs `analyze --pdg`).

## Never Do

- NEVER edit a function, class, or method without first running `impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit changes without running `detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/ThirdBrain/context` | Codebase overview, check index freshness |
| `gitnexus://repo/ThirdBrain/clusters` | All functional areas |
| `gitnexus://repo/ThirdBrain/processes` | All execution flows |
| `gitnexus://repo/ThirdBrain/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
