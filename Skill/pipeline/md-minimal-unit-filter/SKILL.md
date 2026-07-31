---
name: md-minimal-unit-filter
description: |
  递归扫描一个文档目录，过滤掉目录（TOC）/ 索引 / 概览 / 简介 / 预留接口 / 纯 nav 等"非最小单元"内容，保留单 API / 单函数 / 单类型定义类的 Markdown 文件，把它们的绝对路径逐行写入一个 text 文件，供后续 ingest 流水线消费。
  Use when the user says: "筛选最小单元"、"过滤掉目录类文档"、"找出 API 文档"、"filter minimal units"、"scan and pick API docs"、"我要进知识库的 md"。
  Do NOT use for: 在线 URL 抓取（用 web_fetch）、PDF/Word 抽取（用 pdf/docx skill）、结构化抽取（用 api-doc-extractor skill）、全量无差别爬取（用 api-doc-crawler skill）。
---

> **预览说明**：上方 `---` 之间是 YAML frontmatter，所有 markdown 预览器都会把它当元数据**隐藏渲染**——这是设计行为。下方 `<details>` 块把同一份元数据用人类可读的方式再展示一次。

<details>
<summary><strong>📌 Skill 元数据（预览用，与上方 frontmatter 同源）</strong></summary>

| 字段 | 值 |
|---|---|
| **name** | `md-minimal-unit-filter` |
| **description** | 递归扫描一个文档目录，过滤掉目录（TOC）/ 索引 / 概览 / 简介 / 预留接口 / 纯 nav 等"非最小单元"内容，保留单 API / 单函数 / 单类型定义类的 Markdown 文件，把它们的绝对路径逐行写入一个 text 文件，供后续 ingest 流水线消费。 |
| **trigger phrases** | `筛选最小单元`、`过滤掉目录类文档`、`找出 API 文档`、`filter minimal units`、`scan and pick API docs`、`我要进知识库的 md` |
| **not for** | 在线 URL 抓取（用 `web_fetch`）、PDF/Word 抽取（用 `pdf`/`docx` skill）、结构化抽取（用 `api-doc-extractor` skill）、全量无差别爬取（用 `api-doc-crawler` skill） |
| **scope** | project（适用任何含 Markdown 文档树的目录，但内置规则针对 AscendC API 风格） |
| **position in pipeline** | 数据处理层第 1 步（在数据采集层 `api-doc-crawler` 之后、`api-doc-extractor` 之前） |

</details>

<br>

# Markdown 最小单元筛选（md-minimal-unit-filter）

把一个文档树里**真正可以当知识库原料**的 Markdown 文件挑出来，跳过目录/索引/概览/简介/breadcrumb 这类"非最小单元"内容，输出一个 text 文件作为下游管线的输入清单。

---

## 1. 核心原则

- **白名单默认**：默认所有 `.md` 都是候选；只剔除**明确不属于最小单元**的文件
- **覆盖 100%**：扫描必须递归到深层子目录；任何被命中的文件都必须出现在清单里或出现在 `<out>.excluded.txt` 里——不存在"没扫到"
- **路径绝对、UTF-8、逐行**：输出文件每行一条绝对路径，方便下游 `xargs` / Python / LLM 直接消费
- **可解释**：每个被剔除的文件都能说出"为什么"

---

## 2. Inputs to collect

调用前先确认/读取：

- `--src <dir>`：要扫描的根目录（必填）。本仓库常见值：`API参考/`
- `--out <text-file>`：输出清单文件路径（必填），每行一个绝对路径
- `--verbose`：打印每个被剔除的文件 + 原因（默认只打汇总）
- `--dry-run`：只盘点不写文件，先看报告
- `--exclude-pattern <regex>`：额外的文件名剔除正则（可重复）
- `--exclude-h1 <text>`：额外的 H1 标题剔除（可重复，精确匹配）

> 用户没给 `--src` 时，**先问**，不要去扫用户机器上的随机目录。

---

## 3. 剔除规则（按命中顺序）

| # | 维度 | 命中即剔除 | 例子 |
|---|---|---|---|
| 1 | **路径白名单** | 路径中含 `.git` / `.venv` / `node_modules` / `__pycache__` / `.idea` / `.ruff_cache` 等构建/缓存目录 | — |
| 2 | **扩展名** | 不是 `.md` / `.markdown` | — |
| 3 | **文件名精确** | basename 完全等于 `index / readme / summary / overview / toc / allapi / all_api` 等 | `readme.md` |
| 4 | **文件名后缀** | basename 以 `列表 / 一览 / 概览 / 概述 / 总览 / 导航 / 目录 / 简介 / 前言 / 预留接口` 结尾（带 `$` 锚定，**不误伤** `DataTypeList` / `ListTensorDesc` 等真 API） | `AI_CPU_API列表.md`、`Mutex/简介.md`、`附录/预留接口.md` |
| 5 | **H1 标题（精确）** | 首个 `#` 文本等于 `简介 / 概览 / 概述 / 总览 / 前言 / 目录 / 索引 / 汇总 / 导航 / 预留接口 / 废弃接口 / Introduction / Overview / ...` | `# 简介` |
| 6 | **正文 listing 话术** | 含 `本章节列出…` / `本章列出…` / `本章罗列…` | `附录/预留接口.md` |
| 7 | **正文结构** | **无 H4 + 无 API spec 标记 + 链接密度 > 30%** → 视为纯 nav/breadcrumb 页 | `WaitPreBlock.md`（scraping 失败、只剩 nav）、`DataType.md`、`DataTypeList.md` |
| 8 | **额外规则** | `--exclude-pattern` / `--exclude-h1` 命中 | 用户自定义 |

**正向信号（**用以避免误杀**）**：

- 文件含 H4 章节且至少 1 个 H4 命中 API spec 标记（`功能说明 / 函数原型 / 参数说明 / 原型定义 / 函数说明 / 返回值说明 / 约束说明 / 调用示例 / 注意事项 / 需要包含的头文件 / Public成员函数 / ...`）→ **直接 INCLUDE**（不受其它规则影响）
- 文件无 H4、但链接密度 ≤ 30% → INCLUDE（视为"真 API 但 scraping 失败降级"，仍含部分有效文本）

**未被任何规则命中 → 写入清单**。

---

## 4. 关键阈值与原理

| 阈值 | 默认值 | 原理 |
|---|---|---|
| `LINK_DENSITY_THRESHOLD` | `0.30` | 实测：本仓库 290 行纯 nav/breadcrumb 页链接密度稳定在 **31.7%**；含真内容的页 < **20%**；30% 是干净的分界 |

为什么 30% 是好的阈值：
- 真 API（即使 scraping 失败降级）通常含描述/约束/参数说明等文本，链接密度 < 20%
- TOC 表格 + nav 链接组成"超链接海"，链接密度 ≈ 32%
- 30% 留 2% 缓冲，稳健

---

## 5. 脚本实现

实际跑的是 `scripts/filter_md.py`（纯 Python 3.11+，无第三方依赖）。算法：

```
for p in sorted(src.rglob('*')):
    if not p.is_file(): skip
    if p.suffix not in {.md, .markdown}: skip
    if any(parent in JUNK_DIRS for parent in p.parts): skip
    if _classify_filename(p.stem):                       # 规则 3 + 4
        excluded, reason = filename
        continue
    text = p.read_text(utf-8)
    if h1_matches_exclude(text):                         # 规则 5
        excluded, reason = h1
        continue
    if body_matches_listing_hint(text):                  # 规则 6
        excluded, reason = body
        continue
    h4 = H4_RE.findall(text)
    if any(h4 has API spec marker):                      # 正向信号
        included
        continue
    if link_density(text) > 0.30:                        # 规则 7
        excluded, reason = breadcrumb
        continue
    included, reason = 'no H4 but low link density'      # 通过
```

---

## 6. Procedure

按以下顺序执行；任一步失败按"Failure handling"处理。

1. **参数解析 + 工具自检**：`command -v python3`（或 Windows 上 `Get-Command python`）。
2. **盘点源目录**（dry-run 至少跑到这步）：
   - 递归扫 `--src`，按 `.md` / `.markdown` 过滤，跳过构建/缓存目录
   - 统计 `total_candidates / included / excluded`
   - 终端输出一行：`<目录> 共 N 个候选，其中 M 个通过（保留进 KB），K 个剔除（TOC/索引/概览/纯 nav）`
3. **应用剔除规则**（详见 §3）
4. **写文件**（除非 `--dry-run`）：用 `tmp + rename` 原子写，避免下游读到半截
5. **写报告**：`--out` 同目录下写 `<out>.excluded.txt`，每行 `<abs_path>\t<reason>`，方便复查
6. **收尾**：终端打印汇总

---

## 7. Output contract

每次执行产出：

| 文件 | 必有 | 说明 |
|---|---|---|
| `<--out>` 指定的 text 文件 | ✓ | 一行一条绝对路径，UTF-8，无 BOM，行尾 `\n` |
| `<--out>.excluded.txt` | ✓ | 被剔除文件清单，每行 `<abs_path>\t<reason>` |

下游 Markdown → YAML 提取脚本或 `api-doc-extractor` 用 `<--out>` 文本做 input list 即可。

---

## 8. Failure handling

| 错误 | 处理 |
|---|---|
| `--src` 不存在或不是目录 | fail fast，提示用户检查路径 |
| 缺 Python 3 | fail fast，提示装 |
| 单文件读不了（权限/编码） | 标记 `read_error` 计入 excluded + 终端汇总里说"读失败 N 个"；**不中断**整体 |
| 输出文件写不了（权限/磁盘满） | fail fast，不要写半截 |
| `--dry-run` 模式下任何错 | **仍然 fail** |

---

## 9. Examples

### Example 1：扫描 `API参考/` 找出可入库的最小单元

```bash
python3 Skill/pipeline/md-minimal-unit-filter/scripts/filter_md.py \
  --src "API参考/" \
  --out "ingest/output/minimal-units.txt" \
  --verbose
```

预期输出（终端）：
```
[md-minimal-unit-filter] 扫描完成
  src:  /Users/.../API参考
  out:  /Users/.../ingest/output/minimal-units.txt
  total .md files:     2249
  INCLUDED (min unit): 2185
  EXCLUDED (TOC/...):  64
```

`ingest/output/minimal-units.txt` 共 2185 行（每行一个绝对路径）。`ingest/output/minimal-units.txt.excluded.txt` 列出 64 个被剔除的，分类：
- ~30 个 TOC/概览/简介（`filename_suffix` / `h1_exact`）
- ~30 个 scraping 失败只剩 nav 的真 API（`breadcrumb` 链接密度 > 30%）

### Example 2：dry-run，只看汇总不写文件

```bash
python3 Skill/pipeline/md-minimal-unit-filter/scripts/filter_md.py \
  --src "API参考/" \
  --out "ingest/output/minimal-units.txt" \
  --dry-run
```

### Example 3：自定义额外剔除规则

```bash
python3 Skill/pipeline/md-minimal-unit-filter/scripts/filter_md.py \
  --src "API参考/Utils_API/" \
  --out "ingest/output/utils-minimal-units.txt" \
  --exclude-pattern "deprecated|legacy" \
  --exclude-h1 "废弃接口"
```

### Example 4：后续管线消费清单

```bash
# 把清单喂给下游处理
xargs -a ingest/output/minimal-units.txt -I{} echo "{}" \
  | head -5
```

输出示例：
```
/Users/.../API参考/AI_CPU_API/assert.md
/Users/.../API参考/AI_CPU_API/printf.md
/Users/.../API参考/SIMD_API/基础API/工具函数/CeilDivision.md
...
```

---

## 10. Windows (win32) platform notes

核心脚本是跨平台 Python 3，**macOS / Linux / Windows 都可执行**。本节只列需要在 PowerShell 下替换的命令片段：

| macOS / Linux bash | Windows PowerShell 等价 |
|---|---|
| `python3 Skill/.../filter_md.py` | `python Skill\pipeline\md-minimal-unit-filter\scripts\filter_md.py` |
| `command -v python3` | `Get-Command python`（Windows 通常叫 `python`） |
| `--src "API参考/"` | `--src "API参考\"` 或 `--src "API参考"`（Python `Path.rglob` 都吃） |

PowerShell 里 `&&` 是 `;`（不短路）或 `&&`（PS7+），旧版 PS5.1 只能 `;`，写脚本时显式用 `if ($LASTEXITCODE -eq 0) { ... }` 链式判断更稳。

---

## 11. 与本仓库其他 skill 的衔接

```
[数据采集层 · api-doc-crawler]
        │  更新 API参考/**/*.md + state + manifest
        ▼
[md-minimal-unit-filter]   ←   你在这里
        │  产出 minimal-units.txt
        ▼
[Markdown → YAML 提取脚本]
        │  产出 ingest/output/yaml/<doc>.yaml
        ▼
[api-doc-extractor]        →  16 字段结构化契约
        │
        ▼
[ingest → vector store]
```

这个 skill 处于**数据处理层的第一步**，作用是**节流**——把 2249 篇文档里的 64 篇
非最小单元（TOC / 概览 / 简介 / 纯 nav 失败品）先砍掉，避免下游浪费 token 抽它们。
数据采集层的数据爬取、正文解析和目录层级规则见
[`docs/data-collection-layer-crawler.md`](../../../docs/data-collection-layer-crawler.md)。
