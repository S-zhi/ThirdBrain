---
name: api-doc-crawler
description: |
  Pipeline 第 2 步（接 `md-minimal-unit-filter` 的清单）：递归爬取指定目录下所有 AI API 文档（Markdown / HTML / 纯文本），抽取标题、章节、参数表、代码示例、版本与命名空间（org.product.namespace.version）等元数据，输出结构化 YAML/JSON 上下文包喂给下游 `api-doc-extractor`。强契约：必须 100% 覆盖输入清单的所有文档，未覆盖的进 uncovered.md 一一点名。
  Use when the user says: "爬 API 文档"、"把 AI 文档拉下来"、"同步文档目录"、"导出 API 文档"、"刷新语料"、"crawl api docs"、"按清单抽元数据"。
  Do NOT use for: 在线 URL 抓取（用 web_fetch）、二进制 PDF/Word 抽取（用 pdf/docx skill）、单文档片段阅读（用 read 工具）、先筛掉非最小单元（用 md-minimal-unit-filter）、结构化契约抽取（用 api-doc-extractor）。
---

> **预览说明**：上方 `---` 之间是 YAML frontmatter，所有 markdown 预览器（VSCode / GitHub / Obsidian）都会把它当元数据**隐藏渲染**——这是设计行为，Claude / Cursor / Copilot 等 AI 工具通过读它来决定是否触发本 skill。下方 `<details>` 块把同一份元数据用人类可读的方式再展示一次，方便你在编辑器里直接看到。

<details>
<summary><strong>📌 Skill 元数据（预览用，与上方 frontmatter 同源）</strong></summary>

| 字段 | 值 |
|---|---|
| **name** | `api-doc-crawler` |
| **description** | Pipeline 第 2 步（接 `md-minimal-unit-filter` 的清单）：递归爬取指定目录下所有 AI API 文档（Markdown / HTML / 纯文本），抽取标题、章节、参数表、代码示例、版本与命名空间（`org.product.namespace.version`）等元数据，输出结构化 YAML/JSON 上下文包喂给下游 `api-doc-extractor`。强契约：必须 100% 覆盖输入清单的所有文档，未覆盖的进 `uncovered.md` 一一点名。 |
| **trigger phrases** | `爬 API 文档`、`把 AI 文档拉下来`、`同步文档目录`、`导出 API 文档`、`刷新语料`、`crawl api docs`、`按清单抽元数据` |
| **not for** | 在线 URL 抓取（用 `web_fetch`）、二进制 PDF/Word 抽取（用 `pdf`/`docx` skill）、单文档片段阅读（用 `read` 工具）、先筛掉非最小单元（用 `md-minimal-unit-filter`）、结构化契约抽取（用 `api-doc-extractor`） |
| **scope** | project（仅本仓库 `API参考/` 适用） |
| **position in pipeline** | 第 2 步（接 md-minimal-unit-filter，喂 api-doc-extractor） |

</details>

<br>

# API 文档爬取（api-doc-crawler）

把一个目录下的 AI API 文档全部、可验证、幂等地转成结构化上下文包。**完整性是硬契约**：目标目录里能被白名单命中的文档，一个都不能少——漏掉的会在收尾报告 `uncovered.md` 里逐个点名，CI 退出码非 0。

---

## Inputs to collect

调用前先确认/读取：

- `--src <dir>`：要爬的根目录。本仓库默认是 `API参考/`（含 2249 个 markdown）。
- `--out <dir>`：输出目录，默认 `ingest/output/yaml/`。
- `--ext <.md,.markdown,.html,.htm,.txt>`：白名单扩展名，默认 `.md,.markdown`。
- `--incremental`：增量模式（基于 mtime + sha1 跳过未变文件）。
- `--concurrency <N>`：并发 worker 数，默认 `min(8, cpu_count)`。
- `--rate-limit <req/sec>`：URL 拉取限速，默认 `5`。
- `--retry <N>`：单文档失败重试次数，默认 `2`。
- `--exclude <glob>`：额外排除规则，可重复。
- `--skip`：跳过所有交互确认（CI 批处理用），**不影响**任何错误中断。
- `--dry-run`：只盘点不写文件，先看报告再决定。

> 用户没给 `--src` 时，默认走本仓库的 `API参考/`；不要去爬用户机器上的其他目录。

---

## Procedure

按以下顺序执行；任一步失败按"Failure handling"处理，不静默继续。

1. **解析参数 + 工具自检**：先把 `--src` 等参数记下来；再 `command -v python3 rg find` 三件套检查（缺一个就 fail，不要尝试装）。

2. **盘点源目录（这步决定"全部"的定义）**：
   - 递归扫 `--src`，按 `--ext` 白名单过滤；
   - 套 `--exclude`（默认排除 `_*`、`node_modules`、`.git`、`.venv`）；
   - 统计 `total_candidates / by_ext / by_dir`，并跟上一轮 `manifest.json` 对账（增量模式标记 `new / modified / unchanged / deleted`）；
   - 终端输出一行：`<目录> 共 N 个候选，其中 M 个需处理（Δ 新增、≠ 修改）`。
   - **N 之后的任何不一致都要在 uncovered 报告里解释**——这是契约的"锚"。

3. **爬取与抽取**（并发 `--concurrency`）：
   ```
   read → detect_encoding → normalize_text → extract_metadata → extract_sections → extract_code → write_yaml
   ```
   每篇文档至少产出以下字段（缺值用字符串 `_unknown_`，**不要**用 `null`）：
   ```yaml
   doc_id: <sha1(src_relpath)[:16]>
   source_path: <相对 --src 的路径>
   namespace: <com.huawei.cann.ascendc.op.{version} 或 _unknown_>
   sub_namespace: <normalize|linalg|conv|... 或 _unknown_>
   version: <v8|... 或 _unknown_>
   title: <H1>
   sections: [<section_name>, ...]
   code_blocks: N
   hash: <sha256(content)>
   mtime: <ISO 8601>
   scraped_at: <ISO 8601>
   ```
   章节按以下映射抽取（不严格匹配时按 H2/H3 切分保留在 `sections[]`，**不要静默丢字段**）：
   | 章节关键词 | 字段 |
   |---|---|
   | `产品支持情况` | `product_support` |
   | `功能说明` | `description` |
   | `函数原型` | `signatures` |
   | `参数说明` | `params`（结构化 `{name, type, desc}[]`） |
   | `返回值说明` | `returns` |
   | `约束说明` | `constraints` |
   | `调用示例` | `examples` |
   | `需要包含的头文件` | `headers` |
   | `注意事项` | `notes` |

4. **URL 拉取（如 `--src` 是 URL 列表）**：
   - 限速 `--rate-limit`，超时 30s，重试 `--retry` 次；
   - `Content-Type` 不在 `text/markdown | text/html | text/plain | application/xhtml+xml` 内的跳过并记录；
   - 失败 URL 落 `dead-letter/urls.txt`。

5. **写文件**：`tmp + rename` 原子写（避免并发 worker 读到半截）。

6. **校验完整性（**这是 skill 的灵魂**）**：
   ```
   covered    = len(yaml_files_written) + len(dead_letter_files)
   uncovered  = total_candidates - covered - len(explicitly_excluded)
   assert uncovered == 0   # 否则写 uncovered.md 并 exit 1
   ```
   - 通过：写 `manifest.json`（含 `total / by_ext / by_dir / new / modified / unchanged / deleted / failed / duration_sec`）+ `summary.md`（人读小结）。
   - 不通过：写 `uncovered.md` 逐行列出 `expected_path → reason`，**退出码非 0**。
   - **禁止**"补空 yaml 凑数"——那比漏报更糟糕，下游会把它当真文档。

7. **收尾**（增量模式同时输出 `state.db`，记录 sha1 + mtime）：
   - 终端打印：`爬取完成：覆盖 N/M（100%），新增 Δ，修改 ≠，失败 F（已落 dead-letter），耗时 T。`

---

## Output contract

每次执行产出（路径相对于 `--out`）：

| 文件 | 必有 | 说明 |
|---|---|---|
| `<relative_path>.yaml` | ✓ | 每篇文档的结构化包 |
| `manifest.json` | ✓ | 本次爬取清单（统计 + 增量 delta） |
| `summary.md` | ✓ | 人读小结（覆盖数、失败数、平均大小、Top5 慢/快） |
| `dead-letter/` | △ | 失败样本（文件 + URL 分目录） |
| `uncovered.md` | **不漏则无** | 出现 = 事故，CI 挂红 |
| `state.db` | 增量模式 | sha1 + mtime 缓存，下一轮直接复用 |

下游 skill（`ingest-normalizer` 等）默认从 `ingest/output/yaml/` 读，**不要**让下游重新爬。

---

## Failure handling

| 错误 | 处理 |
|---|---|
| 缺工具（python3/rg/find） | fail fast，提示装哪一个，**不要**自动装 |
| 文件读不了（权限/编码） | 进 `dead-letter/<relpath>.err` + 继续 |
| 解析异常 | 标记 `parse_status: failed` + 进 dead-letter |
| URL 拉取失败 | 重试 `--retry` 次仍败 → `dead-letter/urls.txt` |
| 完整性不达标 | `uncovered.md` + `exit 1`（CI 必挂红） |
| 单文件失败 | **不影响**其他文件，整体不中断 |
| `--skip` 模式下任何错 | **仍然 fail**，跳过的是交互确认不是错误 |

空文档也要落 yaml（占位 `parse_status: empty`），不要静默跳过——下游要做"已处理"对账。

---

## Examples

**Input 1**：用户说"爬一下 API参考/ 下的所有文档"

执行（macOS / Linux）：
```bash
python3 ingest/scripts/extract_docs.py \
  --src API参考/ \
  --out ingest/output/yaml/ \
  --ext .md,.markdown \
  --incremental
```

执行（Windows PowerShell）：
```powershell
python ingest/scripts/extract_docs.py `
  --src API参考/ `
  --out ingest/output/yaml/ `
  --ext .md,.markdown `
  --incremental
```

**Output**：
- `ingest/output/yaml/` 下生成 2249 个 yaml（或增量模式下 Δ 个新增 + ≠ 个修改）
- `ingest/output/manifest.json` 写好
- 终端：`爬取完成：覆盖 2249/2249（100%），新增 0，修改 0，失败 0，耗时 47s。`

**Input 2**：CI 漏抓一份文档（`API参考/SIMD_API/基础API/工具函数/Async.md`）

执行同上，结束后：
- `ingest/output/uncovered.md` 出现一行：`API参考/SIMD_API/基础API/工具函数/Async.md → 文件不存在或被 exclude`
- 进程退出码 1，CI 挂红，PR 不可合。

---

## Windows (win32) platform notes

本 skill 的核心脚本 `ingest/scripts/extract_docs.py` 是跨平台 Python 3，**macOS / Linux / Windows 都可执行**。本节只列需要在 PowerShell 下替换的命令片段：

| macOS / Linux bash | Windows PowerShell 等价 |
|---|---|
| `command -v python3` | `Get-Command python`（Windows 通常叫 `python`） |
| `rg --files API参考/` | `Get-ChildItem -Recurse -Include *.md API参考/ \| Select-Object -ExpandProperty FullName` |
| `find API参考/ -name '*.md'` | `Get-ChildItem -Recurse -Filter *.md API参考/` |
| `cat manifest.json \| jq .` | `Get-Content manifest.json \| ConvertFrom-Json` |
| `cp -r src dst/` | `Copy-Item -Recurse src dst` |
| `sha1sum file` | `Get-FileHash file -Algorithm SHA1` |
| `/tmp/...` | `$env:TEMP\...`（用 `Join-Path $env:TEMP "..."`） |

工具自检改为：
```powershell
foreach ($t in @('python','Get-ChildItem','Get-FileHash')) {
  if (-not (Get-Command $t -ErrorAction SilentlyContinue)) { throw "缺少: $t" }
}
```

PowerShell 里 `&&` 是 `;`（不短路）或 `&&`（PS7+），旧版 PS5.1 只能 `;`，写脚本时显式用 `if ($LASTEXITCODE -eq 0) { ... }` 链式判断更稳。
