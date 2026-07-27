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
- **服务**：FastAPI + pydantic v2
- **依赖管理**：`uv`
- **存储**：Postgres + pgvector（默认）/ Qdrant 二选一
- **LLM**：OpenAI / Anthropic / 本地 vLLM 抽象层，按 case 切换
- **质量**：ruff + mypy(strict) + pytest
- **可观测**：OpenTelemetry → Trace 五阶段结构化落库

> 用户拍板前先按默认推进。任何一项被否决，更新本节并同步 `docs/architecture.md`。

## 3. 目录结构

```
.
├── core/            # 领域类型：API Document / Version / Namespace / Contract
├── ingest/          # 文档解析、结构化、版本对齐、增量同步、弃用状态
├── retrieve/        # 意图识别 → 查询构造 → 粗排 → 精排 → 重排
├── context/         # 上下文包构造：候选对比卡、参数契约、反例
├── trace/           # 5 阶段 Trace 数据模型、采集、查询接口
├── benchmark/       # API-RAG Benchmark：cases、runner、归因报告
├── service/         # FastAPI 入口（检索 / 摄取 / 评测 API）
├── cli/             # 维护者 CLI：摄取、跑评测、看 trace
├── tests/
│   ├── unit/        # 单测
│   ├── integration/ # 集成（需本地 Postgres + pgvector）
│   └── benchmark/   # 评测 case（默认禁跑，CI 单独触发）
└── data/            # 离线语料、fixtures（不入仓）
```

## 4. 开发约定

- **分支**：`main` 为发布分支；功能从 `feat/<scope>-<short-desc>` 切出；hotfix 走 `fix/*`
- **提交**：Conventional Commits（`feat:` / `fix:` / `docs:` / `refactor:` / `test:` / `chore:`）
- **代码风格**：
  - Python: `ruff format` + `ruff check`，`line-length = 100`
  - 公共 API 强制 `mypy --strict`
  - 命名空间/版本字符串一律**全小写 + 点分隔**（`com.example.product.api.v2`）
  - 标识符英文；注释与文档中文
- **测试**：
  - 任何新行为必须有单测；新检索策略必须有 Benchmark case
  - 集成测试需 `tests/integration/conftest.py` 中的本地 Postgres+pgvector fixture
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
- [x] Embedding 提供方：**Bailian 千问 qwen3.7-text-embedding**（2560 维，主路径）；本地 sentence-transformers MiniLM（CI/离线 fallback）。`src/dao/emb/embedder.py`
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
