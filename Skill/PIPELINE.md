# RAG 文档摄取 Pipeline 总览

> 本文档解释 `Skill/` 目录下 pipeline 的结构、每个 skill 的职责、上下游衔接，以及当前构建进度。
>
> **配套阅读**：
> - `AGENTS.md`（项目根）— 项目定位 + 5 阶段 Trace + 目录约定
> - `Skill/pipeline/<name>/SKILL.md` — 每个 skill 的独立规约
> - `ingest/scripts/` — 实际可执行脚本

---

## 1. 整体定位

`Skill/pipeline/` 是 **RAG 文档摄取流水线的 skill 化沉淀**：把"原始文档 → 结构化上下文包 → 向量库"这条链路拆成若干个**单一职责、可独立触发**的 skill，串成数据流。每个 skill：

- 都有独立 `SKILL.md`，含 frontmatter（name/description）+ 主体（Inputs/Procedure/Output/Failure）
- 都通过 `lint-skill.js` 校验，遵循 Mavis skill-creator 规范
- 上下游通过**文件清单**（text/json）解耦，不共享内存

**核心原则**：
- **节流优先**：能砍的（TOC/概览/纯 nav）先在 pipeline 第 1 步砍掉，不让下游浪费 token
- **完整性硬契约**：每步都校验 `covered + dead_letter == total`，漏掉的进 `uncovered.md` + exit 1
- **幂等**：每步基于 mtime + sha1 缓存，下一轮只处理 Δ
- **失败隔离**：单文件失败不阻断整体，落 `dead-letter/`

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
    ├── api-doc-crawler/           ← Step 2 ✅ 已建（位置待修正，见 §6）
    │   └── SKILL.md
    │   （依赖 ingest/scripts/extract_docs.py 提供的引擎）
    │
    └── api-doc-extractor/         ← Step 3 ⏳ 待建
        └── （规划中）
```

> ⚠️ **现状说明**：`api-doc-crawler` 之前误放在 `Skill/api-doc-crawler/`，未在 `Skill/pipeline/` 下。文档结尾 §6 会说明修正计划。

---

## 3. Pipeline 数据流

```
                ┌──────────────────────────────────────┐
                │  Step 1 · md-minimal-unit-filter    │
                │  ─────────────────────────────────── │
                │  扫描 API参考/，筛掉 64 篇非最小     │
                │  单元（TOC/概览/简介/纯 nav）        │
                └──────────────┬───────────────────────┘
                               │
                               ▼  ingest/output/minimal-units.txt
                              （每行 1 个绝对路径，共 ~2185 行）
                               │
                ┌──────────────┴───────────────────────┐
                │  Step 2 · api-doc-crawler            │
                │  ─────────────────────────────────── │
                │  按清单爬取，产出 yaml 上下文包       │
                │  + 校验 100% 覆盖                     │
                └──────────────┬───────────────────────┘
                               │
                               ▼  ingest/output/yaml/<doc>.yaml
                              （每篇 1 个 yaml，含元数据 + 章节）
                               │
                ┌──────────────┴───────────────────────┐
                │  Step 3 · api-doc-extractor          │
                │  ─────────────────────────────────── │
                │  把 yaml 抽成 16 字段结构化契约       │
                │  （参数类型/返回类型/约束/示例）      │
                └──────────────┬───────────────────────┘
                               │
                               ▼  ingest/output/extracted/<doc>.json
                              （机器可消费的 RAG 输入）
                               │
                               ▼
                  [embed + index → vector store]
```

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

### 4.2 `api-doc-crawler` — Step 2：爬取 + 元数据抽取 ✅（位置待修正）

| 项 | 内容 |
|---|---|
| **职责** | 拿到 Step 1 的清单后，逐文件抽取**元数据**（标题、命名空间、版本、章节、代码块数、hash、mtime），输出**yaml 上下文包** |
| **输入** | `--src <dir>` 或 URL 列表文件<br>`--out <dir>`（默认 `ingest/output/yaml/`）<br>`--ext`（默认 `.md,.markdown`）<br>`--incremental`（基于 sha1+mtime 跳过未变） |
| **输出** | `<doc>.yaml` × N（每篇 1 个）<br>`manifest.json`（统计 + 增量 delta）<br>`summary.md`（人读小结）<br>`dead-letter/`（失败样本）<br>`uncovered.md`（**漏抓时出现，CI 挂红**） |
| **关键约束** | 100% 覆盖（`covered + dead_letter == total`，否则 fail）<br>缺值用 `_unknown_` 字符串，不用 `null`<br>写文件用 `tmp + rename` 原子写 |
| **位置（待修正）** | 现：`Skill/api-doc-crawler/`<br>应为：`Skill/pipeline/api-doc-crawler/`（跟 md-minimal-unit-filter 同级） |
| **脚本依赖** | `ingest/scripts/extract_docs.py`（实际跑的工作引擎） |

**和 Step 1 的关系**：本 skill 既可以**直接吃 `--src <dir>`**（自己递归），也可以**吃 Step 1 的清单**（`--src ingest/output/minimal-units.txt`）——后者是推荐路径，因为少了 64 篇噪音。

---

### 4.3 `api-doc-extractor` — Step 3：结构化契约抽取 ⏳

| 项 | 内容 |
|---|---|
| **职责** | 把 Step 2 产出的 yaml（已经含元数据 + 章节），进一步**结构化抽取**为机器可消费的 16 字段契约（参数类型 / 返回类型 / 约束 / 示例代码） |
| **输入** | Step 2 产出的 `ingest/output/yaml/<doc>.yaml` |
| **输出（规划）** | `ingest/output/extracted/<doc>.json`（每篇 1 个）<br>+ `contracts.jsonl`（合并的 JSONL，方便下游 batch） |
| **设计原则** | 这一步是**LLM 主导**：纯规则抽不出"参数语义" / "约束意图" / "反例"——必须用 LLM 协助<br>但 LLM 输出**必须**带置信度 + 引用原文（行号 / 锚点）<br>任何 LLM 失败 → fall back 到 `_LLM_PENDING` 占位，绝不编造 |
| **状态** | 待建（先等 Step 2 跑稳，把 yaml schema 冻结后再设计 prompt + 验证集） |

**为什么需要它**：Step 2 的 yaml 是"文档原貌"——章节还在 markdown 形态。下游 RAG 检索时，需要的是 `param.name = "dst"` / `param.type = "uint8_t*"` 这种**结构化字段**，做精确过滤 / namespace 隔离 / 版本判断。Step 3 就是这个"从 markdown 到结构化"的桥梁。

---

## 5. 端到端执行（一次完整跑完）

```bash
# === Step 1：筛最小单元（产出清单）===
python3 Skill/pipeline/md-minimal-unit-filter/scripts/filter_md.py \
  --src "API参考/" \
  --out "ingest/output/minimal-units.txt" \
  --verbose

# === Step 2：按清单爬取 + 抽元数据（产出 yaml）===
python3 ingest/scripts/extract_docs.py \
  --src "$(cat ingest/output/minimal-units.txt)" \
  --out "ingest/output/yaml/" \
  --ext .md,.markdown \
  --incremental

# === Step 3（待建）：结构化抽取 ===
# python3 Skill/pipeline/api-doc-extractor/scripts/extract.py \
#   --in ingest/output/yaml/ \
#   --out ingest/output/extracted/
```

**预期终端输出**：
```
[Step 1] 扫描完成：2185 INCLUDED / 64 EXCLUDED
[Step 2] 爬取完成：覆盖 2185/2185（100%），新增 Δ，修改 ≠，失败 F
[Step 3] 抽取完成：2185 extracted / 0 failed / 平均 12.3s/篇
```

---

## 6. 当前进度 & 待办

| # | Skill | 状态 | 待办 |
|---|---|---|---|
| 1 | `md-minimal-unit-filter` | ✅ 已建，已有脚本可用 | — |
| 2 | `api-doc-crawler` | ✅ SKILL.md 写好 | ⚠️ **位置需从 `Skill/api-doc-crawler/` 移到 `Skill/pipeline/api-doc-crawler/`**；description 里"第 1 个 skill"应改为"第 2 个" |
| 3 | `api-doc-extractor` | ⏳ 待建 | 等 Step 2 跑稳后设计 prompt + 验证集 |
| 4 | `vector-indexer`（规划） | ⏳ 待建 | 把 Step 3 产出 embed + 落向量库（Postgres+pgvector / Qdrant） |
| 5 | `retriever`（规划） | ⏳ 待建 | 用户查询 → 意图识别 → 粗排 → 精排 → 上下文包 |

**位置修正方案**（下一步要做的）：

```
# 1. 创建正确位置的目录
mkdir -p Skill/pipeline/api-doc-crawler

# 2. 移动 SKILL.md
mv Skill/api-doc-crawler/SKILL.md Skill/pipeline/api-doc-crawler/SKILL.md

# 3. 删除空目录
rmdir Skill/api-doc-crawler

# 4. 更新 SKILL.md description：把"第 1 个 skill"改成"第 2 个 skill"
#    把"pipeline 的入口"改成"pipeline 的第 2 步（接 md-minimal-unit-filter 的清单）"
```

执行后再跑一遍 `node lint-skill.js` 校验。

---

## 7. 各 skill 上下游衔接表

| 上游 skill | 产出 | 下游 skill 消费方式 |
|---|---|---|
| （人工）`API参考/` | 2249 个 `.md` | `md-minimal-unit-filter` 用 `--src` 扫 |
| `md-minimal-unit-filter` | `ingest/output/minimal-units.txt` | `api-doc-crawler` 用 `--src <清单>` 喂入 |
| `api-doc-crawler` | `ingest/output/yaml/<doc>.yaml` × N | `api-doc-extractor` 逐个读 |
| `api-doc-extractor` | `ingest/output/extracted/<doc>.json` | `vector-indexer` embed + 落库 |
| `vector-indexer` | pgvector / Qdrant collection | `retriever` 按 user query 召回 |

---

## 8. 相关文档

| 文档 | 作用 |
|---|---|
| `AGENTS.md`（项目根） | 项目定位、5 阶段 Trace、技术栈 |
| `Skill/PIPELINE.md` | **本文档**，pipeline 总览 |
| `Skill/pipeline/<name>/SKILL.md` | 各 skill 的独立规约（frontmatter + 主体） |
| `ingest/scripts/extract_docs.py` | Step 2 的实际引擎 |
| `ingest/output/manifest.json` | Step 2 跑完后生成的清单 |
| `docs/architecture.md`（待建） | 5 阶段 Trace 的详细设计、数据流图 |
| `docs/data-model.md`（待建） | API Document / Context Package / Trace 字段定义 |

---

## 9. 一句话总结

> **Pipeline = 3 个 skill 串成一条线**：① `md-minimal-unit-filter` 把 2249 篇砍到 2185 篇真 API，② `api-doc-crawler` 把每篇抽成 yaml 上下文包（100% 覆盖硬契约），③ `api-doc-extractor`（待建）把 yaml 抽成 16 字段结构化契约喂给向量库。每步都有自己的 SKILL.md，串起来的依据是**文件清单 + 完整性校验**，不共享内存、不互相耦合。
