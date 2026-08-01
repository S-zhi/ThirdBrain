---
name: api-doc-pipeline-recovery
description: |
  RAG 文档摄取流水线（doc_sync → extract_docs → fix_signatures）跑挂/半挂时的应急恢复工具集。
  覆盖 4 类已知故障：JS 渲染未完成、namespace 字段缺失、磁盘满、LLM 速率限制。
  Use when the user says: "抓取失败"、"fail 比例高"、"namespace 缺失"、"磁盘满"、"rate limit"、"重抓失败页面"、"补跑 extract"、"流水线挂了"、"pacing"、"scanning stuck"。
  Do NOT use for: 正常增量同步（用 api-doc-crawler skill）、文档检索（用 api-doc-retriever skill）、结构化抽取本身（用 api-doc-extractor skill）。
---

> **预览说明**：上方 `---` 之间是 YAML frontmatter，所有 markdown 预览器都会把它当元数据**隐藏渲染**——这是设计行为。下方 `<details>` 块把同一份元数据用人类可读的方式再展示一次。

<details>
<summary><strong>📌 Skill 元数据（预览用，与上方 frontmatter 同源）</strong></summary>

| 字段 | 值 |
|---|---|
| **name** | `api-doc-pipeline-recovery` |
| **description** | RAG 文档摄取流水线（doc_sync → extract_docs → fix_signatures）跑挂/半挂时的应急恢复工具集。覆盖 4 类已知故障：JS 渲染未完成、namespace 字段缺失、磁盘满、LLM 速率限制。 |
| **trigger phrases** | `抓取失败`、`fail 比例高`、`namespace 缺失`、`磁盘满`、`rate limit`、`重抓失败页面`、`补跑 extract`、`流水线挂了` |
| **not for** | 正常增量同步（用 `api-doc-crawler`）、文档检索（用 `api-doc-retriever`）、结构化抽取本身（用 `api-doc-extractor`） |
| **scope** | project（针对本仓库 doc_sync + extract_docs 流水线） |
| **position in pipeline** | 横向 recovery skill，覆盖采集层（crawler）和处理层（extractor）两端 |

</details>

<br>

# API 文档摄取流水线 · 应急恢复

> **核心定位**：本 skill 是**操作手册**而非**设计规范**。其他 skill（`api-doc-crawler`、`api-doc-extractor`）描述系统正常工作的契约；本 skill 描述系统**跑挂/半挂**时怎么救。
>
> 4 个故障案例来自 2026-08-01 的真实运行，每个都附**根因 + 修复 patch + 验证步骤**。

---

## 1. 故障速查表

| 故障 | 症状 | 速查 |
|---|---|---|
| **① JS 渲染未完成** | `failed 比例 > 30%` 错误 = `AdapterError: 动态 HTML 中无法唯一定位正文 ... 命中 0 个候选` | [§ 3](#3-故障①js-渲染未完成) |
| **② namespace 字段缺失** | B2 失败 = `Schema 2.1 Pipeline 失败: documents[0].namespace 必须是非空字符串` | [§ 4](#4-故障②namespace-字段缺失) |
| **③ 磁盘满** | 错误 = `OSError: [Errno 28] No space left on device`；sync_docs 触发 `block_apply: true` 但 `failed < 25%` 时**部分**写盘 | [§ 5](#5-故障③磁盘满) |
| **④ LLM 速率限制** | B2 stderr = `[MiniMax额度] 当前窗口剩余 0%`；`MiniMax API 错误 2062: 已达到 Token Plan 速率限制` | [§ 6](#6-故障④llm-速率限制minimax) |

---

## 2. 三步应急总控

`scripts/master.sh` 是流水线总控，跑 A2 (doc_sync) → B1 (备份 yaml) → B2 (extract_docs) → C (fix_signatures) 全套，**脱手 20h**。每阶段 10h 硬上限。

```bash
# 启动全量流水线
nohup Skill/pipeline/api-doc-pipeline-recovery/scripts/master.sh \
  > data/pipeline/master.stdout 2> data/pipeline/master.stderr < /dev/null &
echo $! > data/pipeline/master.pid
disown
```

**关键文件**：
- `data/pipeline/pipeline.log` — 每阶段开始/结束/退出码
- `data/pipeline/FINAL_REPORT.md` — 跑完时自动写
- `data/pipeline/{a2,b2,c}_*.{stdout,stderr,pid,running}` — 各阶段状态

**断点续跑**：B2 阶段用 `_pending.txt` 跳过已成功的 md，重跑时 `--overwrite` 不传即可。

---

## 3. 故障①：JS 渲染未完成

### 症状

```text
[error] hiascend-cann-910beta3/atlasascendc_api_07_01234: AdapterError:
       动态 HTML 中无法唯一定位正文 'atlasascendc_api_07_01234': 命中 0 个候选
```

`failed / discovered > 30%`。

### 根因

华为云文档页是 **JS 动态渲染**——打开页面时浏览器先拿到 HTML 空壳，JS 跑起来才把正文内容塞进 HTML。`playwright.page.goto(..., wait_until="domcontentloaded")` 只等 HTML 骨架到位，**JS 还没跑**。`wait_for(state="attached")` 也只等元素挂上 DOM，**不保证内容已渲染**。

### 修复

**两处改动**：

**(a) `configs/document_sync.yaml`** — `wait_until` 改 networkidle
```yaml
browser:
  channel: chrome
  headless: true
- wait_until: domcontentloaded
+ wait_until: networkidle   # 等所有网络请求完成（JS 拉数据完）
  navigation_timeout_ms: 60000
  selector_timeout_ms: 60000
```

**(b) `src/doc_sync/adapters/hiascend.py` `_fetch_browser_once`** — 30s 内容兜底
```python
# 改后: 在 article_heading.wait_for 之后, 等正文真有文字
article_body_selector = self.options.selectors.article_body
article_heading = page.locator(f"{article_body_selector} h1")
await article_heading.first.wait_for(state="attached", timeout=60_000)

# 30s 硬上限, 超时按当前 page.content 抓 (不阻塞整批)
_content_deadline = time.monotonic() + 30.0
_content_filled = False
while time.monotonic() < _content_deadline:
    _text_len = await page.evaluate(
        """(selector) => {
            const body = document.querySelector(selector);
            return body ? (body.innerText || '').trim().length : 0;
        }""",
        article_body_selector,
    )
    if _text_len > 100:
        _content_filled = True
        break
    await asyncio.sleep(0.5)
if not _content_filled:
    logger.warning(
        "doc_sync 内容兜底 30s 超时: %s 正文 innerText<100 字符, 按当前 page.content 抓取",
        ref.canonical_uri,
    )
rendered_html = await page.content()
```

**关键**：用 `time.monotonic()` 自己计 30s，**不抛 Playwright 异常**，避免触发上层 HTTP fallback 走静态 HTML（静态 HTML 没 JS 渲染内容，注定抓不到正文）。

### 验证

```bash
# 跑 100 个 dry-run 验证
PYTHONUNBUFFERED=1 uv run python -m src.script.sync_docs sync \
  --config configs/document_sync.yaml \
  --dry-run --trigger manual --limit 100 \
  > data/pipeline/smoke.stdout 2> data/pipeline/smoke.stderr
```

**期望**：`failed: 0` 或 < 5%。修前是 57% failed。

### 实际效果

- 修前：`discovered 2505, failed 1428 (57%)`
- 修后：`discovered 99, failed 0`（100 页 smoke test）

---

## 4. 故障②：namespace 字段缺失

### 症状

B2 跑完 `yaml/_batch_state.json` 看到：

```text
failed: 904
  658 个 Schema 2.1 Pipeline 失败: documents[0].namespace 必须是非空字符串
  195 个 MiniMax API 错误 2056/2062 (LLM 速率限制)
  ...
```

`yaml/` 下只剩 1345 个 yaml（**只有 SIMD_API 成功**），其他模块全军覆没。

### 根因

`src/script/extract_docs.py:1043` 的 `resolve_authoritative_hints` 函数**只处理了 SIMD_API**：

```python
if "SIMD_API" in path_parts:
    hints["namespace"] = f"{SIMD_NAMESPACE_PREFIX}.{hints['version']}"
```

**没有 SIMT_API / Utils_API / AI_CPU_API / 附录 / Ascend_C_API列表.md 分支**。batch 模式不传 `--namespace` CLI 参数，namespace 只能从 markdown 头部 + 路径推断——这些模块推不出来。

### 修复

**(a) 加 namespace prefix 常量** (`src/script/extract_docs.py` line 85 附近)

```python
SIMD_NAMESPACE_PREFIX = "com.huawei.cann.ascendc.op"
SIMD_CATEGORY_BY_DIRECTORY = {"工具函数": "function"}

# 其他模块的 namespace 推断规则（基于 URL 路径归属 + 产品约定猜测）
SIMT_NAMESPACE_PREFIX = "com.huawei.cann.ascendc.op"   # SIMT 与 SIMD 同属 Ascend C, 共用 prefix
UTILS_NAMESPACE_PREFIX = "com.huawei.cann.ascendc.utils"
AICPU_NAMESPACE_PREFIX = "com.huawei.cann.ascendc.aicpu"
APPENDIX_NAMESPACE_PREFIX = "com.huawei.cann.ascendc.appendix"
OVERVIEW_NAMESPACE_PREFIX = "com.huawei.cann.ascendc.overview"
```

**(b) 加分支** (`resolve_authoritative_hints` 函数)

```python
path_parts = set(document_path.parts)
if "SIMD_API" in path_parts:
    hints["language"] = hints["language"] or "cpp"
    if hints["namespace"] is None and hints["version"] is not None:
        hints["namespace"] = f"{SIMD_NAMESPACE_PREFIX}.{hints['version']}"
    if hints["category"] is None:
        for directory, category in SIMD_CATEGORY_BY_DIRECTORY.items():
            if directory in path_parts:
                hints["category"] = category
                break
elif "SIMT_API" in path_parts:
    hints["language"] = hints["language"] or "cpp"
    if hints["namespace"] is None and hints["version"] is not None:
        hints["namespace"] = f"{SIMT_NAMESPACE_PREFIX}.{hints['version']}"
elif "Utils_API" in path_parts:
    # ... 同上
elif "AI_CPU_API" in path_parts:
    # ...
elif "附录" in path_parts:
    # ...
else:
    if hints["namespace"] is None and hints["version"] is not None:
        hints["namespace"] = f"{OVERVIEW_NAMESPACE_PREFIX}.{hints['version']}"
```

**注意**：上面这些 prefix 是**猜测**——华为云文档没明文规定 namespace 命名。SIMT 与 SIMD 同 URL 路径 (`/ascendcopapi/`)，**共用 prefix** 是合理推断。错了再改——只是查询时的 namespace 过滤会不准，**不影响 yaml 内容**。

### 验证

```bash
PYTHONUNBUFFERED=1 uv run python -c "
from src.script.extract_docs import (
    read_markdown, extract_source_url, resolve_authoritative_hints
)
from pathlib import Path
import argparse
args = argparse.Namespace(chunk_id=None, name=None, namespace=None, version=None,
    language=None, category=None, module=None, source_revision=None)
md_path, md = read_markdown(Path('API参考/SIMT_API/SIMD与SIMT混合编程简介/扩展语法/内存空间限定符.md'))
url = extract_source_url(md)
hints = resolve_authoritative_hints(args, md_path, md, url)
print(f'namespace: {hints.get(\"namespace\")!r}')"
# 期望: namespace: 'com.huawei.cann.ascendc.op.910beta3'
```

---

## 5. 故障③：磁盘满

### 症状

```text
[error] atlasascendc_api_07_10053: OSError: [Errno 28] No space left on device
[error] atlasascendc_api_07_10108: OSError: [Errno 28] No space left on device
```

rendered_html 缓存堆到 18GB，占满 12GB 剩余空间。

### 根因

`rendered_html` 缓存是抓取时的 HTML 全文，每个页面 100KB-8MB。**全量抓 2500 页面** = 4-20GB。磁盘 12GB 剩的时候后期 fetch 写盘失败。

`partial_run.max_failure_ratio: 0.25, block_apply: true` 触发判断：

- `failed / discovered < 25%` → **不**触发 block_apply → 正常 apply 写盘
- `failed / discovered > 25%` → 触发 block_apply → **整批不写盘**（已写 staging 但不写到 API参考/）

### 修复

**(a) 清 rendered_html 释放空间**

```bash
mavis-trash data/doc_sync/rendered_html  # 释放 18GB+
```

下次跑会重建缓存。

**(b) sync_docs 已经写 staging 但没 apply 写盘**——从 staging 复制

sync_docs 的 apply 阶段是先写 staging 再写 API参考/。如果 apply 阶段卡了/失败，**1239 个 md 在 staging 里 ready**，但 API参考/ 没拿到。

```bash
# staging 路径模式:
# data/doc_sync/staging/<run_id>/<source_id>/API参考/<子目录>/<file>.md
SRC=data/doc_sync/staging/<run_id>/<source_id>/API参考
rsync -avhi "$SRC/" API参考/    # -i --stats 看哪些 skip
```

**关键检查**：
- `ls API参考/AI_CPU_API/` 看 mtime 是不是新 — 14:22 是 sync_docs 抓的时间
- `find API参考 -name "*.md" | wc -l` 数最终数量 = 2249 baseline + (added + restored)
  - `updated` = **覆盖**式更新，**不增加**文件数
  - `added + restored` = 新增文件数

### 验证

```bash
# 抽样对比 staging 和 API参考/ 的 md5 一致性
python3 -c "
import os, subprocess, random
random.seed(42)
staging_root = 'data/doc_sync/staging/<run_id>/<source_id>/API参考'
all_md = []
for root, _, files in os.walk(staging_root):
    for f in files:
        if f.endswith('.md'):
            all_md.append(os.path.join(root, f))
samples = random.sample(all_md, 5)
match = 0
for s in samples:
    rel = s.replace(staging_root + '/', '')
    target = 'API参考/' + rel
    s_md5 = subprocess.run(['md5', '-q', s], capture_output=True, text=True).stdout.strip()
    t_md5 = subprocess.run(['md5', '-q', target], capture_output=True, text=True).stdout.strip()
    if s_md5 == t_md5:
        match += 1
print(f'match: {match}/5')
"
# 期望: 5/5
```

---

## 6. 故障④：LLM 速率限制（MiniMax）

### 症状

B2 stderr 反复刷：

```text
[MiniMax额度] 当前窗口剩余 0%，约 1小时30分钟后刷新
```

B2 stdout 末尾：

```text
"MiniMax API 错误 2062: 已达到 Token Plan 速率限制：
 请升级 Token Plan 套餐或切换为按量付费 API 使用。"
```

### 根因

MiniMax API 有**窗口速率限制**（每小时 100% 用完）和**周配额**（57% 剩）。

`extract_docs.py` 收到 2062 后**直接放弃**剩下的页（**没有 retry-with-backoff**）。所以窗口额度耗尽时 = 0 成功。

### 修复

短期：**等 1h30m 窗口刷新后用 `_pending.txt` 重跑失败的 904 个 md**

```bash
# _pending.txt 包含所有失败文件的绝对路径
PYTHONUNBUFFERED=1 uv run python -m src.script.extract_docs \
  --batch-file yaml/_pending.txt \
  --output-dir yaml \
  --workers 2 \
  > data/pipeline/b2_rerun.stdout 2> data/pipeline/b2_rerun.stderr
# 不传 --overwrite: 已有 yaml 校验通过的会自动跳过
```

**`scripts/retry_failed_pages.py` 不适用于 LLM 速率限制**（那是 doc_sync 抓 HTML 的工具）。LLM 速率限制用上面这个 extract_docs 重跑命令。

长期（如果频繁撞墙）：改 `extract_docs.py` 给 `call_minimax` 加 retry-with-backoff + 退避到窗口刷新。但这是 `src/script/extract_docs.py` 的代码改动——**慎重评估再动**。

### 验证

```bash
# 等窗口刷新后跑
PYTHONUNBUFFERED=1 uv run python -m src.script.extract_docs \
  --batch-file yaml/_pending.txt --output-dir yaml --workers 2

# 期望: _pending.txt 自动清空，_batch_state.json status=completed
```

---

## 7. 精准重抓 failed 页面

`scripts/retry_failed_pages.py` 是**只重抓指定的几个 URL**——不是全量重跑。适用于：

- sync_docs 跑完后**有少量 failed**（比如 10 个磁盘满失败的）
- 想精准补几个特定 URL，不要重新 fetch 2000+ 个 unchanged

```bash
# 1. 改 FAILED_URLS 列表（脚本顶部）
# 2. 跑
PYTHONUNBUFFERED=1 uv run python \
  Skill/pipeline/api-doc-pipeline-recovery/scripts/retry_failed_pages.py
```

**典型耗时**：10 个 URL × 5-10s = 50-100s。**比 sync_docs 全量 2h 快 100x**。

**注意**：脚本里 relative_path 默认 `API参考/SIMT_API/{page_id}.md`——根据实际模块调整。retry_failed_pages.py 是**临时工具**，不是通用 production 脚本。

---

## 8. fix_signatures.py 兜底

`scripts/fix_signatures.py` 扫 `yaml/` 下所有 yaml，对 `documents[].use.function_details.signature.value == ""` 的项，**从对应 markdown 兜底提取函数原型**。

```bash
uv run python Skill/pipeline/api-doc-pipeline-recovery/scripts/fix_signatures.py \
  --yaml-dir yaml --md-dir API参考 --report data/pipeline/c_report.txt
```

**适用场景**：B2 跑完后部分 yaml 的 `signature` 字段是空（主流程漏提），用正则从 markdown 兜底。

**典型命中率**：2-5%（大部分空 signature 是说明性文件，本来就没原型）。

---

## 9. 实战时间线（2026-08-01 真实跑过）

```
02:54  cron 启动 master.sh（A2 → B1 → B2 → C 全套）
05:11  A2 完成：partial 1428 failed (57%)，block_apply 触发
08:31  B2 完成：1345/2249 成功 (60%)，904 failed (709 namespace bug + 195 LLM 限速)
08:33  C 完成：fixed 2
11:47  诊断：发现 namespace bug + 1428 failed 都是 wait_until 太早
12:14  修代码：wait_until=networkidle + 30s 兜底
12:14  smoke test 100 个：0 failed ✅
12:25  全量 doc_sync 启动
14:22  全量完成：2245 discovered, 10 failed (磁盘满), 0 block_apply 触发
14:30  清 rendered_html + cp staging → API参考/
14:50  retry 10 个 failed：10/10 成功
```

**总产出**：
- API参考/ 2261 个 md（baseline 2249 + 全量 +12）
- 1345 个 yaml（schema 2.1）
- 333 个 yaml 仍空 signature（说明性文件，无原型）

---

## 10. 跨项目可迁移的经验

| 经验 | 跨项目适用 |
|---|---|
| **JS 渲染页面必须 `wait_until: networkidle` + 内容兜底** | ✅ 任何用 Playwright 抓 JS 页的项目 |
| **磁盘满时优先清缓存**（rendered_html / 下载文件）而不是删除产物 | ✅ |
| **3 阶段流水线（fetch + LLM 抽 + 兜底）必须每阶段独立 checkpoint** | ✅ |
| **大文件传输（1239 个 md）靠 staging + journal，磁盘满时从 staging 手工恢复** | ✅ |
| **`failed / discovered < 25%` 不触发 block_apply**（partial 仍可写盘） | ✅ sync_docs 自家语义 |

---

## 11. 与其他 skill 的衔接

```
[api-doc-crawler]                ← 正常增量同步
        │ 故障① / 故障③        修代码 / 修配置
        ▼
[api-doc-pipeline-recovery]      ← 你在这里
        │  scripts/master.sh           脱手 20h 总控
        │  scripts/retry_failed_pages.py  精准重抓
        │  scripts/fix_signatures.py    signature 兜底
        ▼
[api-doc-extractor ⏳]            ← 修复 namespace bug
        │ 故障②
        ▼
[api-doc-retriever]
```
