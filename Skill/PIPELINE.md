# RAG 文档摄取 Pipeline 总览

> 本文档解释 `Skill/` 目录下 pipeline 的结构、每个 skill 的职责、上下游衔接，以及当前构建进度。
>
> **配套阅读**：
> - `AGENTS.md`（项目根）— 项目定位 + 5 阶段 Trace + 目录约定
> - `Skill/pipeline/<name>/SKILL.md` — 每个 skill 的独立规约
> - `src/script/` — 实际可执行脚本（来源同步和 Markdown → YAML）

---

## 1. 整体定位

`Skill/pipeline/` 是 **RAG 文档摄取流水线的 skill 化沉淀**：把“官方来源 → 来源 Markdown → 结构化上下文包 → 向量库”这条链路拆成若干个**单一职责、可独立触发**的 skill，串成数据流。其最前面的来源同步属于数据采集层，后面的筛选和结构化属于数据处理层。每个 skill：

- 都有独立 `SKILL.md`，含 frontmatter（name/description）+ 主体（Inputs/Procedure/Output/Failure）
- 都通过 `lint-skill.js` 校验，遵循 Mavis skill-creator 规范
- 上下游通过**文件清单**（text/json）解耦，不共享内存

**核心原则**：
- **节流优先**：能砍的（TOC/概览/纯 nav）先在 pipeline 第 1 步砍掉，不让下游浪费 token
- **完整性硬契约**：每步都校验 `covered + dead_letter == total`，漏掉的进 `uncovered.md` + exit 1
- **幂等**：来源同步使用规范正文 SHA-256 + JSON state；本地提取步骤按各自稳定摘要只处理 Δ
- **失败隔离**：单文件失败不阻断整体，落 `dead-letter/`

数据采集层的数据爬取模块只输出 Markdown、state 和 manifest；它不生成 YAML、不调用
LLM，也不写 MongoDB/Zvec。详细的抓取、解析、层级和恢复流程见
[`docs/data-collection-layer-crawler.md`](../docs/data-collection-layer-crawler.md)。

---

## 2. 目录结构

```
Skill/
├── PIPELINE.md                    ← 本文档
└── pipeline/                      ← 所有 pipeline skill 都放这里
    │
    ├── md-minimal-unit-filter/    ← Step 1 ✅ 已建
    │   ├── SKILL.md
    │   └── scripts/filter_md.py
    │
    ├── api-doc-crawler/           ← Source Sync ✅ 已建
    │   └── SKILL.md
    │   （实现位于 src/doc_sync/，配置见 configs/document_sync.yaml）
    │
    └── api-doc-extractor/         ← Step 3 ⏳ 待建
        └── （规划中）
```

> **现状说明**：`api-doc-crawler` 的 Skill 规约位于 `Skill/pipeline/api-doc-crawler/`，实现位于 `src/doc_sync/`；不要把 Skill 文件路径和 Python 实现路径混为一谈。

---

## 3. Pipeline 数据流

```
数据采集层 · Source Sync
┌─────────────────────────────────────────────────────────┐
│ api-doc-crawler                                         │
│ YAML → AdapterFactory → 发现/抓取/解析/目录规范化        │
│ → SHA-256 Diff → Markdown + state + manifest             │
└───────────────────────────────┬─────────────────────────┘
                                │ API参考/**/*.md
                                ▼
数据处理层 · Step 1
┌─────────────────────────────────────────────────────────┐
│ md-minimal-unit-filter                                  │
│ 扫描同步后的 API参考/，筛掉 TOC/概览/简介/纯 nav         │
└───────────────────────────────┬─────────────────────────┘
                                │ ingest/output/minimal-units.txt
                                ▼
数据处理层 · Step 2
┌─────────────────────────────────────────────────────────┐
│ Markdown → YAML/文档中间模型（现有提取脚本）             │
└───────────────────────────────┬─────────────────────────┘
                                │ ingest/output/yaml/<doc>.yaml
                                ▼
数据处理层 · Step 3
┌─────────────────────────────────────────────────────────┐
│ api-doc-extractor（规划）                               │
│ YAML → 参数/返回/约束/示例等结构化契约                   │
└───────────────────────────────┬─────────────────────────┘
                                ▼
                       [embed + index → vector store]
```

关键顺序是：先刷新来源 Markdown，再筛选和结构化。`api-doc-crawler` 不按最小单元清单
“爬取并产出 YAML”；它按 source 配置发现官网页面并维护来源副本，YAML 是后续处理阶段的
产物。

---

## 4. 各 Skill 详解

### 4.1 `md-minimal-unit-filter` — Step 1：筛最小单元 ✅

| 项 | 内容 |
|---|---|
| **职责** | 递归扫文档目录，把 TOC / 索引 / 概览 / 简介 / 预留接口 / 纯 nav 这些"非最小单元"剔除，只保留"单 API / 单函数 / 单类型"级别的 Markdown，输出一个**绝对路径清单** |
| **输入** | `--src <dir>`（如 `API参考/`）<br>`--out <text-file>`（如 `ingest/output/minimal-units.txt`）<br>`--exclude-pattern <regex>` / `--exclude-h1 <text>`（可选） |
| **输出** | `<out>` 文本：每行 1 个绝对路径<br>`<out>.excluded.txt`：被剔除文件 + 剔除原因（`abs_path\treason`） |
| **节流效果** | 2249 → 2185 篇（砍掉 64 篇非最小单元，省 2.8% token；如果不砍，下游会把 64 篇 TOC 当真文档抽，召回时噪音大幅上升） |
| **关键阈值** | 链接密度 > 30% → 视为纯 nav → 排除<br>（实测：纯 nav 页 ≈ 31.7%，真 API 降级页 < 20%） |
| **失败处理** | 读失败 → 进 `excluded.txt` 标 `read_error` + 继续；不死锁不中断 |
| **位置** | `Skill/pipeline/md-minimal-unit-filter/` |
| **脚本** | `Skill/pipeline/md-minimal-unit-filter/scripts/filter_md.py`（纯 Python 3，零三方依赖） |

**为什么需要它**：2249 篇里混着 `附录/预留接口.md`、`Mutex/简介.md` 这种"半成品/导航"页。直接喂下游抽取，token 浪费 + 召回噪音 + embedding 干扰。先在这里干净地切掉，下游就能专注"真 API"。

---

### 4.2 `api-doc-crawler` — Source Sync：官网 Markdown 增量同步 ✅

| 项 | 内容 |
|---|---|
| **职责** | 通过 `SourceAdapter` Factory 从官方来源发现文档，用规范正文 SHA-256 对比现有 Markdown，只更新真实变化 |
| **输入** | `configs/document_sync.yaml`；每个 source 声明 Adapter、目标目录、allowlist 和请求策略 |
| **输出** | 更新后的 `API参考/**/*.md`、`data/doc_sync/runs/<run_id>/manifest.json`、每个 source 的 JSON state |
| **关键约束** | 核心逻辑来源无关；内容不变不写文件；单页失败保留旧文件；只有 404/410 累计缺失；所有写入先 staging 再原子替换 |
| **位置** | Skill：`Skill/pipeline/api-doc-crawler/`；实现：`src/doc_sync/`；CLI：`src/script/sync_docs.py` |
| **配置文档** | `docs/document-sync-setup.md` |

**和数据处理层的关系**：本 Skill 位于本地摄取 Pipeline 之前，先刷新
`API参考/`；`md-minimal-unit-filter` 再扫描同步后的目录生成最小单元清单。目录层级、
正文解析、Hash 和恢复策略见 [`docs/data-collection-layer-crawler.md`](../docs/data-collection-layer-crawler.md)。

---

### 4.3 `api-doc-extractor` — 数据处理层 Step 3：结构化契约抽取 ⏳

| 项 | 内容 |
|---|---|
| **职责** | 把 Markdown → YAML 阶段产出的文档原貌，进一步**结构化抽取**为机器可消费的 16 字段契约（参数类型 / 返回类型 / 约束 / 示例代码） |
| **输入** | `ingest/output/yaml/<doc>.yaml` |
| **输出（规划）** | `ingest/output/extracted/<doc>.json`（每篇 1 个）<br>+ `contracts.jsonl`（合并的 JSONL，方便下游 batch） |
| **设计原则** | 这一步是**LLM 主导**：纯规则抽不出"参数语义" / "约束意图" / "反例"——必须用 LLM 协助<br>但 LLM 输出**必须**带置信度 + 引用原文（行号 / 锚点）<br>任何 LLM 失败 → fall back 到 `_LLM_PENDING` 占位，绝不编造 |
| **状态** | 待建（先等 Markdown → YAML 阶段跑稳，把 yaml schema 冻结后再设计 prompt + 验证集） |

**为什么需要它**：Markdown → YAML 的结果是“文档原貌”——章节还在 markdown 形态。下游 RAG 检索时，需要的是 `param.name = "dst"` / `param.type = "uint8_t*"` 这种**结构化字段**，做精确过滤 / namespace 隔离 / 版本判断。Step 3 就是这个“从 markdown 到结构化”的桥梁。

---

## 5. 端到端执行（一次完整跑完）

```bash
# === Source Sync：刷新官方 Markdown（首次先 dry-run）===
uv run python -m src.script.sync_docs sync --apply

# === Step 1：筛最小单元（产出清单）===
python3 Skill/pipeline/md-minimal-unit-filter/scripts/filter_md.py \
  --src "API参考/" \
  --out "ingest/output/minimal-units.txt" \
  --verbose

# === Step 2：Markdown → YAML 文档原貌（数据处理层）===
python3 src/script/extract_docs.py \
  --batch-file "ingest/output/minimal-units.txt" \
  --output-dir "ingest/output/yaml/"

# === Step 3（待建）：结构化抽取 ===
# python3 Skill/pipeline/api-doc-extractor/scripts/extract.py \
#   --in ingest/output/yaml/ \
#   --out ingest/output/extracted/
```

**预期终端输出**：
```
[Source Sync] manifest：discovered N / added Δ / updated Δ / unchanged N / failed F
[Step 1] 筛选完成：2185 INCLUDED / 64 EXCLUDED
[Step 2] YAML 原貌完成：processed 2185 / failed F
[Step 3] 抽取完成：2185 extracted / 0 failed / 平均 12.3s/篇
```

---

## 6. 当前进度 & 待办

| # | 阶段 | 状态 | 待办 |
|---|---|---|---|
| 采集 | `api-doc-crawler` | ✅ Factory、HTTP 基类、Hiascend Adapter、CLI、JSON state/manifest 已建 | 首次全量 bootstrap 前先审核 dry-run manifest，并完成浏览器容量测试 |
| 1 | `md-minimal-unit-filter` | ✅ 已建，已有脚本可用 | — |
| 2 | Markdown → YAML（现有脚本） | ✅ 可运行 | 统一输出 schema 和增量状态 |
| 3 | `api-doc-extractor` | ⏳ 待建 | 等 Markdown → YAML 阶段稳定后设计 prompt + 验证集 |
| 4 | `vector-indexer`（规划） | ⏳ 待建 | 把 Step 3 产出 embed + 落向量库（Postgres+pgvector / Qdrant） |
| 5 | `retriever`（规划） | ⏳ 待建 | 用户查询 → 意图识别 → 粗排 → 精排 → 上下文包 |

首次上线按 `bootstrap --dry-run` → 审核 manifest 和目录层级 → `bootstrap --apply` 执行，
后续由服务器 CronJob 每天运行 `sync --apply --trigger scheduled`；来源同步完成后再运行
筛选和结构化处理。

---

## 7. 各 skill 上下游衔接表

| 上游 skill | 产出 | 下游 skill 消费方式 |
|---|---|---|
| `api-doc-crawler` | 更新的 `API参考/**/*.md` + `manifest.json` | `md-minimal-unit-filter` 扫刷新后的目录 |
| `md-minimal-unit-filter` | `ingest/output/minimal-units.txt` | 现有结构化提取脚本按清单处理 |
| 结构化提取脚本 | `ingest/output/yaml/<doc>.yaml` × N | `api-doc-extractor` / ingest 逐个读 |
| `api-doc-extractor` | `ingest/output/extracted/<doc>.json` | `vector-indexer` embed + 落库 |
| `vector-indexer` | pgvector / Qdrant collection | `retriever` 按 user query 召回 |

---

## 8. 相关文档

| 文档 | 作用 |
|---|---|
| `AGENTS.md`（项目根） | 项目定位、5 阶段 Trace、技术栈 |
| `Skill/PIPELINE.md` | **本文档**，pipeline 总览 |
| `Skill/pipeline/<name>/SKILL.md` | 各 skill 的独立规约（frontmatter + 主体） |
| `src/script/sync_docs.py` | 官网 Markdown 同步 CLI |
| `configs/document_sync.yaml` | SourceAdapter 和同步策略配置 |
| `docs/data-collection-layer-crawler.md` | 数据采集层数据爬取模块的架构、解析、层级、Hash 和恢复流程 |
| `docs/document-sync-setup.md` | 初始化、Cron、恢复和 Adapter 扩展 |
| `data/doc_sync/runs/<run_id>/manifest.json` | 每次同步的通用变更清单 |
| `docs/architecture.md`（待建） | 5 阶段 Trace 的详细设计、数据流图 |
| `docs/data-model.md`（待建） | API Document / Context Package / Trace 字段定义 |

---

## 9. 一句话总结

> **Pipeline = 数据采集 + 本地筛选 + 结构化抽取**：数据采集层的 `api-doc-crawler`
> 先通过 Adapter Factory 和 SHA-256 增量刷新 Markdown，
> `md-minimal-unit-filter` 再筛最小单元，后续提取脚本生成机器可消费的 YAML。
