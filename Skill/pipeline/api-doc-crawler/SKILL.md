---
name: api-doc-crawler
description: |
  在数据采集层从配置的官方文档源定时发现、抓取、解析并增量同步 Markdown。使用
  SourceAdapter Factory 支持多来源，通过规范正文 SHA-256 判定 added/updated/unchanged，
  保留官网目录层级并生成通用
  manifest.json。Use when the user says: "爬 API 文档"、"同步文档目录"、
  "刷新语料"、"定时更新官网文档"、"crawl api docs"。
  Do NOT use for: 结构化 API 契约抽取、向量写入、LLM 回填、PDF/Word 抽取。
---

# API 文档定时同步

本 Skill 对应数据采集层的数据爬取模块，是后续数据处理 Pipeline 的输入阶段。它维护
可追踪的来源 Markdown、目录层级和同步 manifest，不生成结构化 YAML，不调用 LLM，也不
写 MongoDB/Zvec。完整中文设计、解析流程和边界见 [`docs/data-collection-layer-crawler.md`](../../../docs/data-collection-layer-crawler.md)。

## 输入、输出和边界

```text
YAML source 配置
  → AdapterFactory
  → 发现/抓取/解析/层级规范化
  → SHA-256 Diff 和原子应用
  → API参考/**/*.md + state + manifest
  → 下游 md-minimal-unit-filter / 结构化提取
```

采集模块不负责把 Markdown 抽成 API 参数、示例或约束契约；这些内容由后续数据处理层
完成。采集模块必须保证页面正文、来源身份、目录路径和 Hash 可复现。

## 实现入口

- CLI：`python -m src.script.sync_docs`
- 配置：`configs/document_sync.yaml`
- 核心：`src/doc_sync/`
- 初始化文档：`docs/document-sync-setup.md`

## 设计契约

1. 来源实现必须继承 `SourceAdapter` 或 `HttpDocumentSourceAdapter`。
2. Adapter 通过 `AdapterFactory.register()` 显式注册，禁止 YAML 动态导入类。
3. 核心状态、Hash、Diff、写入和 manifest 不包含具体产品字段。
4. 来源专属字段只能进入 `metadata`。
5. 文档身份是 `(source_id, document_id)`，不是文件路径。
6. 是否更新只由规范正文 SHA-256 决定。
7. 内容不变时不得写文件，也不得改变 mtime。
8. 所有候选先进入 staging，正式写入使用同目录临时文件加 `os.replace()`。
9. 单页失败不得覆盖旧文件。
10. 只有明确 404/410 才累计缺失；候选归档也不自动删除文件。

## 文档解析和目录层级

Adapter 的解析必须区分“来源正文”和“最终文件包装”：

- `article_body` 选择器定位正文，标题从配置的 title selectors 提取。
- 清理脚本、样式、无关导航和重复页脚，规范 Unicode、换行、空白和代码块。
- `normalized_content` 用于 SHA-256；`artifact_content` 用于写入 Markdown。
- 文档身份使用稳定 `(source_id, document_id)`，不使用路径作为身份。
- 新页面路径优先复用同一身份的旧路径，其次从页面 title 和父级链接恢复层级，最后才进入 `_待归类`。

官网的 child-to-parent 导航必须转换为 parent-to-child。例如：

```text
SIMD API → C API → Memory 矢量计算 → Exp
```

应生成 `API参考/SIMD_API/C_API/Memory矢量计算/Exp.md`，不能把全部页面平铺在
`API参考/`。当前 Hiascend Adapter 支持浏览器渲染；`browser.rendered_html_directory`
只用于排障，渲染缓存不是最终输出。

公共 HTTP 能力的适用范围：默认 `HttpDocumentSourceAdapter.fetch()` 使用共享
`HttpFetchClient`；当前 Hiascend 为支持 JavaScript 渲染而覆盖浏览器 `fetch`，其 robots、
QPS、429/5xx 重试和响应大小策略需要单独补齐和验收，不能仅根据继承关系假定已经生效。

## 初始化

先读取并检查配置：

```bash
uv run python -c "
from src.doc_sync.config import load_document_sync_config
from src.doc_sync.adapters import AdapterFactory
config = load_document_sync_config('configs/document_sync.yaml')
AdapterFactory.validate_sources(config.sources)
print(AdapterFactory.available_types())
"
```

小范围 dry-run：

```bash
uv run python -m src.script.sync_docs bootstrap --dry-run --limit 10
```

全量 dry-run：

```bash
uv run python -m src.script.sync_docs bootstrap --dry-run
```

人工检查 manifest 后建立状态：

```bash
uv run python -m src.script.sync_docs bootstrap --apply
```

## 日常同步

```bash
uv run python -m src.script.sync_docs sync --apply --trigger scheduled
```

推荐服务器 Cron：

```cron
CRON_TZ=Asia/Shanghai
0 3 * * * cd /opt/ragWithColdApiDocument && uv run python -m src.script.sync_docs sync --apply --trigger scheduled
```

不传 `--apply` 时默认只生成 staging 和 manifest，不修改 Markdown 或 state。

## Hash 与状态

同步过程使用 SHA-256：

- `response_hash`：原始 HTTP 响应，只用于排障。
- `content_hash`：Adapter 输出的规范正文，是更新判定依据。
- `file_hash`：最终 Markdown 字节，用于识别本地修改。

通用操作：

| 操作 | 条件 | 行为 |
|---|---|---|
| `added` | 新稳定身份 | 创建 Markdown |
| `unchanged` | 来源和本地均未变化 | 不写文件 |
| `updated` | 来源正文 Hash 变化 | 原子覆盖 |
| `restored` | 来源未变、本地被修改 | 按策略恢复来源 |
| `moved` | 身份不变、URI 变化 | 更新来源 URI |
| `failed` | 抓取或解析失败 | 保留旧文件 |
| `missing` | 明确 404/410 | 累计缺失次数 |
| `archived_candidate` | 达到缺失阈值 | 保留文件，只标候选 |

持久状态为：

```text
data/doc_sync/state/<source_id>.json
```

禁止重新引入 mtime + SHA-1 或 `state.db` 作为更新依据。

## 输出契约

```text
data/doc_sync/
├── state/<source_id>.json
├── runs/<run_id>/manifest.json
├── staging/<run_id>/
├── backups/<run_id>/
├── journals/<run_id>.json
├── latest.json
└── sync.lock
```

`manifest.json` 至少包含：

- `run_id/mode/trigger/status`
- `stats`
- `updated_markdown`
- `source_results`
- `documents`
- `errors`

服务端消费 `updated_markdown` 即可获得本轮实际落盘的 Markdown 列表。来源专属字段
只能出现在 `documents[].metadata`。

查看本轮真实变化：

```bash
jq '.updated_markdown' data/doc_sync/latest.json
jq '.documents[] | select(.operation != "unchanged") |
  {operation, relative_path, old_content_hash, new_content_hash, reason}' \
  data/doc_sync/latest.json
```

`updated_markdown` 为空表示本轮没有实际写入；失败、404 和 dry-run 候选仍需查看
`documents` 与 `errors`。

## Failure handling

| 错误 | 处理 |
|---|---|
| YAML 无效或未知字段 | 写文件前 fail fast |
| 未知 Adapter | 报错并列出可用类型 |
| URL 越过 allowlist | 拒绝请求 |
| 429/5xx/网络错误 | 有限重试，最终标记单页 failed |
| 正文选择器失效 | 保留旧文件并在 manifest 报错 |
| 浏览器启动/关闭或单页渲染超时 | 当前页 failed，保留旧文件；检查浏览器依赖、复用策略和容量 |
| 单页 404/410 | 增加 missing_count，不删除 |
| 路径逃逸 | 拒绝候选 |
| 目标路径碰撞 | 按配置追加 document_id 或失败 |
| 文件锁冲突 | CLI 退出码 3 |
| 应用阶段中断 | 使用 journal resume |

恢复命令：

```bash
uv run python -m src.script.sync_docs resume --run-id <run_id>
```

## 新增来源

1. 定义 `extra="forbid"` 的 Adapter options。
2. 继承 `SourceAdapter`；网站来源优先继承 `HttpDocumentSourceAdapter`。
3. 实现抽象方法与 URL allowlist。
4. 声明唯一 `adapter_type` 和 `config_model`。
5. 在 `src/doc_sync/adapters/__init__.py` 显式注册。
6. 在 `configs/document_sync.yaml` 增加 source。
7. 运行 Adapter Contract 测试。

核心同步服务禁止增加按具体 adapter type 分支。

新增 Adapter 完成后先用 10 页 dry-run 审核 `relative_path`、正文 Hash 和错误，再做全量
bootstrap。错误平铺副本应移动到 `data/doc_sync/quarantine/<run_id>/` 并生成
`quarantine_manifest.json`，保持可恢复，不能无记录删除。
