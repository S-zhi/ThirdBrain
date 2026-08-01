# 文档定时同步初始化与运维

> 本文是字段级初始化和运维手册。若需要理解“数据采集层 → 数据爬取模块”的完整架构、文档解析和目录层级规则，请先阅读 [`data-collection-layer-crawler.md`](./data-collection-layer-crawler.md)。

本文说明如何配置、初始化和运行可扩展文档同步框架。框架负责从外部来源发现文档、
抓取/渲染页面、解析正文和目录层级、计算正文 SHA-256、增量更新 Markdown，并生成服务端可消费的 `manifest.json`。

框架属于数据采集层的数据爬取模块：它只负责形成可追踪的 Markdown 来源副本。
框架不生成结构化 API YAML，不调用 LLM，也不更新 MongoDB 或 Zvec；这些能力属于后续数据处理层。

## 1. 运行结构

```text
configs/document_sync.yaml
  ↓
AdapterFactory → SourceAdapter
  ↓
发现 → 抓取 → 解析 → SHA-256 → Diff
  ↓
data/doc_sync/staging/<run_id>
  ↓
API参考/**/*.md + manifest.json + state
```

运行时目录：

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

`staging` 是候选文件区，`backups` 用于恢复，`state` 是下一次 Diff 的持久基线，
`runs/<run_id>/manifest.json` 是一次运行的审计快照。历史上发现的错误平铺副本应移入
`data/doc_sync/quarantine/<run_id>/` 并保留 `quarantine_manifest.json`，不要直接删除。

`data/` 已被 Git 忽略。服务端必须把 `data/doc_sync` 放在持久化磁盘上，并确保运行用户
对 `API参考/` 和 `data/doc_sync/` 有读写权限。

## 2. 安装与配置检查

安装锁文件中的依赖：

```bash
uv sync
```

默认配置文件是：

```text
configs/document_sync.yaml
```

只检查 Python 导入、YAML 和 Adapter 注册，不访问官网：

```bash
uv run python -c "
from src.doc_sync.config import load_document_sync_config
from src.doc_sync.adapters import AdapterFactory
config = load_document_sync_config('configs/document_sync.yaml')
AdapterFactory.validate_sources(config.sources)
print(AdapterFactory.available_types())
"
```

## 3. 顶层配置

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---:|---|---|
| `schema_version` | string | 是 | `1.0` | 配置 Schema；未知版本直接失败 |
| `workspace_root` | path | 否 | `..` | 相对于 YAML 所在目录解析 |
| `runtime` | mapping | 否 | 见下文 | 状态、暂存、备份和锁 |
| `http_defaults` | mapping | 否 | 见下文 | HTTP Adapter 的公共请求策略 |
| `policies` | mapping | 否 | 见下文 | 来源无关的同步决策 |
| `sources` | list | 是 | 无 | 一个或多个文档来源 |

所有配置模型都使用 `extra="forbid"`。字段拼写错误不会被忽略，而是在任务写文件前
直接失败。

### 路径解析

`workspace_root` 首先相对于 YAML 文件解析。其他相对路径再相对于
`workspace_root` 解析，不依赖执行命令时的当前工作目录。

例如默认配置位于 `configs/document_sync.yaml`：

```yaml
workspace_root: ..
```

解析结果就是项目根目录。以下两种调用得到相同路径：

```bash
uv run python -m src.script.sync_docs bootstrap --dry-run
cd /tmp
/path/to/project/.venv/bin/python -m src.script.sync_docs bootstrap \
  --config /path/to/project/configs/document_sync.yaml \
  --dry-run
```

## 4. runtime

```yaml
runtime:
  root_directory: ./data/doc_sync
  retention_days: 30
  lock_timeout_seconds: 0
```

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---:|---|
| `root_directory` | path | `./data/doc_sync` | 所有运行制品的根目录 |
| `retention_days` | int ≥ 1 | `30` | runs、staging、backups 保留天数 |
| `lock_timeout_seconds` | number ≥ 0 | `0` | 获取进程锁最多等待时间；0 表示立即失败 |

`root_directory` 必须位于 `workspace_root` 内，且不能和任何 source 的
`target_directory` 相互包含。

## 5. HTTP 默认策略

```yaml
http_defaults:
  user_agent: rag-with-cold-api-doc-sync/0.1
  concurrency: 4
  requests_per_second: 2
  timeout_seconds: 30
  max_response_bytes: 10485760
  respect_robots_txt: true
  retry:
    max_attempts: 4
    initial_backoff_seconds: 1
    max_backoff_seconds: 30
    jitter_ratio: 0.2
    retry_status_codes: [429, 500, 502, 503, 504]
```

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---:|---|
| `user_agent` | string | 项目标识 | 所有 HTTP 请求的 User-Agent |
| `concurrency` | int ≥ 1 | `4` | 最大并发请求数 |
| `requests_per_second` | number > 0 | `2` | 所有 HTTP Adapter 共用的 QPS |
| `timeout_seconds` | number > 0 | `30` | 单请求超时 |
| `max_response_bytes` | int ≥ 1 | 10 MiB | 单页面最大响应 |
| `respect_robots_txt` | bool | `true` | 遵守站点 robots.txt |

`retry` 子字段：

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---:|---|
| `max_attempts` | int ≥ 1 | `4` | 包含首次请求在内的最大尝试次数 |
| `initial_backoff_seconds` | number > 0 | `1` | 第一次重试的退避时间 |
| `max_backoff_seconds` | number > 0 | `30` | 指数退避上限 |
| `jitter_ratio` | number，0–1 | `0.2` | 退避时间随机抖动比例 |
| `retry_status_codes` | list[int] | `429,500,502,503,504` | 允许重试的 HTTP 状态码 |

429 优先使用 `Retry-After`。其他可重试错误使用指数退避和随机抖动。达到
`max_attempts` 后只把当前文档标记为 `failed`，不会删除或覆盖旧文件。

## 6. 通用同步策略

```yaml
policies:
  missing_threshold: 3
  overwrite_local_changes: true
  apply_valid_changes_on_partial_run: true
  redirects:
    max_redirects: 3
    allow_cross_host: false
  large_change:
    warning_ratio: 0.10
    block_apply: false
  partial_run:
    max_failure_ratio: 0.25
    block_apply: true
  path_collision:
    strategy: append_document_id
```

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---:|---|
| `missing_threshold` | int ≥ 1 | `3` | 连续明确 404/410 后标记候选归档 |
| `overwrite_local_changes` | bool | `true` | 来源未变但本地被编辑时恢复来源内容 |
| `apply_valid_changes_on_partial_run` | bool | `true` | 部分页面失败时仍应用其他有效页面 |
| `redirects.max_redirects` | int ≥ 0 | `3` | 最大重定向次数 |
| `redirects.allow_cross_host` | bool | `false` | 是否允许跨 Host；仍需通过 Adapter allowlist |
| `large_change.warning_ratio` | number，0–1 | `0.10` | 变化比例达到阈值时写 `large_change=true` |
| `large_change.block_apply` | bool | `false` | 是否因为大批量变化停止落盘 |
| `partial_run.max_failure_ratio` | number，0–1 | `0.25` | 失败页面占比超过此值时触发安全闸门 |
| `partial_run.block_apply` | bool | `true` | 高失败比例时是否禁止应用有效候选 |
| `path_collision.strategy` | enum | `append_document_id` | 路径冲突时追加稳定文档 ID；也可设 `fail` |

只有明确 HTTP 404/410 才累计 `missing_count`。目录中没有发现、超时、429 和 5xx
都不算删除证据。达到阈值后文件仍保留，只在 state 和 manifest 中标记
`archived_candidate`。

## 7. sources

每个 source 独立配置、独立状态、独立目标目录：

```yaml
sources:
  - id: hiascend-cann-910beta3
    enabled: true
    target_directory: ./API参考
    adapter:
      type: hiascend
      options: {}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `id` | string | 是 | 稳定 source id；允许小写字母、数字、点、下划线和连字符 |
| `enabled` | bool | 否，默认 `true` | 是否参与默认运行 |
| `target_directory` | path | 是 | Markdown 目标目录 |
| `adapter.type` | string | 是 | Factory Registry 中的类型 |
| `adapter.options` | mapping | 否，默认 `{}` | 由对应 Adapter 的 Pydantic 模型校验 |

source id 不能重复，不同 source 的目标目录不能相同或互相包含。

只运行一个 source：

```bash
uv run python -m src.script.sync_docs sync \
  --source hiascend-cann-910beta3 \
  --dry-run
```

同一命令可以重复使用 `--source` 选择多个来源。

## 8. hiascend Adapter

`hiascend` options 的完整字段：

| 字段 | 类型 | 必填/默认值 | 说明 |
|---|---|---|---|
| `product` | string | 必填 | 产品名；只写入 `metadata` |
| `version` | string | 必填 | 文档版本；只写入 `metadata` |
| `language` | string | 必填 | 文档语言；只写入 `metadata` |
| `root_urls` | list[string] | 必填，至少 1 项 | 新页面发现入口 |
| `allowed_hosts` | list[string] | 必填，至少 1 项 | 只填写 Host，不含 scheme 和路径 |
| `allowed_path_prefixes` | list[string] | 必填，至少 1 项 | 以 `/` 开头的产品/版本路径白名单 |
| `document_url_pattern` | regex string | 必填 | 对 URL 文件名执行的文档正则 |
| `max_pages` | int ≥ 1 | `3000` | 单次最大页面数 |
| `selectors` | mapping | 见子字段默认值 | HTML 解析选择器 |
| `selectors.article_body` | CSS selector | `.the-article-body` | 正文容器 |
| `selectors.title` | list[CSS selector] | `h1`, `.topictitle1` | 按顺序尝试标题 |
| `selectors.parent_links` | list[CSS selector] | `.familylinks a` | 提取目录层级的父级链接 |
| `browser` | mapping | 可选 | 页面依赖 JavaScript 时使用浏览器渲染 |
| `browser.channel` | string | `chrome` | Playwright 浏览器通道 |
| `browser.headless` | bool | `true` | 是否无头运行 |
| `browser.wait_until` | enum | `domcontentloaded` | 页面导航等待条件 |
| `browser.navigation_timeout_ms` | int > 0 | `60000` | 页面导航超时 |
| `browser.selector_timeout_ms` | int > 0 | `60000` | 正文选择器等待超时 |
| `browser.retry_attempts` | int 1–5 | `2` | 浏览器抓取失败后的尝试次数 |
| `browser.retry_initial_backoff_seconds` | number > 0 | `1` | 浏览器重试初始退避；指数退避上限受运行环境约束 |
| `browser.fallback_to_http` | bool | `true` | 浏览器限流/超时后是否切换共享 HTTP Client |
| `browser.rendered_html_directory` | path | `./data/doc_sync/rendered_html` | 渲染 HTML 缓存，仅用于排障和复现 |
| `existing_document` | mapping | 必填 | 现有 Markdown 元信息解析规则 |
| `existing_document.source_url_pattern` | regex string | 必填 | 必须包含命名组 `value` |
| `existing_document.external_id_pattern` | regex string | 必填 | 必须包含命名组 `value` |
| `output` | mapping | 见子字段默认值 | 最终 Markdown 输出规则 |
| `output.source_label` | string | `来源` | 来源 URI 行标签 |
| `output.external_id_label` | string | `节点` | 外部 ID 行标签 |
| `output.unresolved_directory` | relative path | `_待归类` | 新页面无法分类时的目录 |

Host、路径前缀和文件名正则三者必须同时通过，才能发起请求。跨版本 URL 不会被
当前 source 接受。仓库初始化正则同时覆盖官网顶层 `.html` 页面和 `context/`
下的嵌套 `.md` 页面，因此当前 2249 份 Markdown 都能建立唯一注册表。

浏览器抓取遇到 403、429、5xx、正文选择器超时或浏览器连接异常时，会先按
`retry_attempts` 退避重试；仍失败且 `fallback_to_http=true` 时，自动使用共享 HTTP
连接池获取官网 SSR HTML。降级结果会在文档 manifest metadata 写入
`fetch_mode=http_fallback`、`degraded=true` 和 `degrade_reason`。HTTP 降级也失败时，
才把该页面标记为 `failed` 并保留旧文件。

修改正文选择器时先运行：

```bash
uv run python -m src.script.sync_docs sync --dry-run --limit 10
```

检查 manifest 中是否出现“正文选择器未命中”或大批量 `updated`。不要直接用
`--apply` 验证新选择器。

### 8.1 目录层级和 Markdown 路径

新页面的路径不是简单使用页面标题平铺生成。Hiascend Adapter 依次使用：

1. 同一 `document_id` 现有 Markdown 的目录路径；
2. 页面 `<title>` 中的“大标题 - 中标题 - 小标题 - 文档标题”段落；
3. `selectors.parent_links` 提取的父级链接；
4. 无法判断时的 `output.unresolved_directory`（默认 `_待归类`）。

例如官网结构：

```text
SIMD API → C API → Memory 矢量计算 → Exp
```

应落为：

```text
API参考/SIMD_API/C_API/Memory矢量计算/Exp.md
```

页面 `<title>` 中识别到的 child-to-parent 目录段会转换为 parent-to-child，父级链接则按
选择器返回的 DOM 顺序去重；且不会把当前页面标题重复追加到父级目录。路径会拒绝绝对路径、`..` 和控制字符；同名冲突默认追加
稳定 `document_id`，也可以将 `path_collision.strategy` 改为 `fail`。

如果发现页面被平铺，先检查 dry-run manifest 的 `documents[].relative_path`、渲染缓存和
页面标题，再修改选择器或 Adapter 规则；不要直接批量移动 Markdown，否则会破坏稳定身份
和 state 的映射。

## 9. 首次初始化

第一步只读检查少量页面：

```bash
uv run python -m src.script.sync_docs bootstrap --dry-run --limit 10
```

第二步全量 dry-run：

```bash
uv run python -m src.script.sync_docs bootstrap --dry-run
```

检查：

- `stats.discovered` 是否符合预期。
- `errors` 是否为空。
- `documents[].relative_path` 是否沿用现有路径。
- 新页面是否进入正确目录或 `_待归类`。
- `large_change` 是否符合预期。

确认后建立状态并应用真实差异：

```bash
uv run python -m src.script.sync_docs bootstrap --apply
```

旧格式 Markdown 如果包含官网导航，只要规范正文能够在旧文件中完整对齐，就只建立
基线，不重写文件。无法对齐的页面进入 `updated`。

## 10. 日常运行

手工运行：

```bash
uv run python -m src.script.sync_docs sync --apply
```

默认不传 `--apply` 时等同 dry-run。

CLI 退出码：

| 退出码 | 含义 |
|---:|---|
| `0` | 全部成功，包括无变化 |
| `1` | 配置或系统级失败 |
| `2` | 部分成功，存在单页失败 |
| `3` | 另一个任务持有同步锁 |

## 11. Manifest

每次运行写：

```text
data/doc_sync/runs/<run_id>/manifest.json
data/doc_sync/latest.json
```

服务端通常只需要：

- `status`
- `stats`
- `updated_markdown`
- `documents`
- `errors`

`updated_markdown` 只包含本轮实际写入的 Markdown。dry-run 时为空，候选变化仍记录在
`documents[].operation`。

查看本轮发生变化的文档：

```bash
jq '.updated_markdown' data/doc_sync/latest.json
jq '.documents[] | select(.operation != "unchanged") |
  {operation, relative_path, document_id, old_content_hash, new_content_hash, reason}' \
  data/doc_sync/latest.json
```

其中 `updated_markdown` 为空表示没有文件真正写入；它不代表没有失败、404 或 dry-run
候选。要区分官网变化和本地人工修改，分别查看 `old_content_hash/new_content_hash`
以及 `old_file_hash/new_file_hash`。

来源专属字段只能出现在 `documents[].metadata`，通用字段不依赖任何产品或版本。

## 12. 定时任务

Linux Cron：

```cron
CRON_TZ=Asia/Shanghai
0 3 * * * cd /opt/ragWithColdApiDocument && uv run python -m src.script.sync_docs sync --apply --trigger scheduled
```

Kubernetes CronJob 关键字段：

```yaml
spec:
  schedule: "0 3 * * *"
  timeZone: Asia/Shanghai
  concurrencyPolicy: Forbid
```

容器命令：

```yaml
command:
  - uv
  - run
  - python
  - -m
  - src.script.sync_docs
  - sync
  - --apply
  - --trigger
  - scheduled
```

Kubernetes 仍应挂载持久化的 `data/doc_sync` 和 Markdown 目标目录。程序自己的
`sync.lock` 会防止定时任务与人工任务重叠。

浏览器渲染模式按 source 生命周期复用一个 Playwright 浏览器进程和上下文：每个页面只创建
和关闭 `Page`，source 完成（成功、部分失败或异常）后才释放浏览器资源。这样可以把启动
开销摊到整个 source。注意：动态浏览器路径当前不经过 `HttpFetchClient`，因此
`http_defaults` 的 robots、QPS、429/5xx 重试和响应大小闸门不会自动作用于浏览器请求；
若生产要求统一策略，应在浏览器路径增加同等封装并单独验收。外部 Cron/Kubernetes 负责
job 超时，应用自身仍通过单页超时和错误隔离保证任务可恢复。上线前仍应在目标机器做容量
测试；浏览器复用不能消除网络慢、选择器失效或页面超时。

## 13. 保留与凭证

`retention_days` 会清理过期的 `runs/`、`staging/`、`backups/` 和 `journals/`。
`state/`、`latest.json` 与 `sync.lock` 不按年龄清理：state 是后续 Diff 的持久基线，
`latest.json` 始终指向最近一次运行。生产环境还应对整个运行目录做磁盘快照或备份。

错误路径或历史平铺副本的清理使用可恢复 quarantine：

```text
data/doc_sync/quarantine/<run_id>/
├── <原相对路径的副本>
└── quarantine_manifest.json
```

manifest 应记录原路径、新路径、来源身份、SHA-256 和移动时间。确认新结构稳定且备份
策略有效后，才按 retention 策略清理 quarantine；不得用无记录的递归删除代替迁移。

YAML 会拒绝以下明文键及 `access_token`、`client_secret` 等复合形式：

```text
api_key authorization cookie credential password secret secret_key token
```

需要凭证的未来 Adapter 只能声明环境变量名：

```yaml
credential_env: INTERNAL_DOC_API_TOKEN
```

真实值由服务端 Secret 注入环境变量。Adapter 不得把值写入日志、state 或 manifest。

## 14. 中断恢复

应用阶段每完成一个文件都会刷新：

```text
data/doc_sync/journals/<run_id>.json
```

恢复：

```bash
uv run python -m src.script.sync_docs resume --run-id <run_id>
```

resume 只重新应用 journal 中 `completed=false` 且 staging 仍存在的动作，同时把
`completed=true` 但中断前尚未持久化的 state 补齐，不重新访问来源。如果所需 staging
已被清理，resume 返回失败；此时重新运行正常 `sync --apply`。

## 15. 新增 Adapter

实现步骤：

1. 定义 `extra="forbid"` 的 Adapter options 模型。
2. 继承 `SourceAdapter`；HTTP 站点优先继承 `HttpDocumentSourceAdapter`。
3. 实现 bootstrap、initial_refs、fetch、parse、discover_refs 和路径建议。
4. 声明唯一 `adapter_type` 和 `config_model`。
5. 在 `src/doc_sync/adapters/__init__.py` 显式注册。
6. 在 YAML 增加 source。
7. 运行 Adapter Contract 测试。

最小结构：

```python
class ExampleOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root: str


class ExampleAdapter(HttpDocumentSourceAdapter):
    adapter_type = "example"
    config_model = ExampleOptions

    # 实现所有抽象方法和 is_allowed_uri()


AdapterFactory.register(ExampleAdapter)
```

核心同步服务不得增加 `if adapter_type == "example"`。

## 16. Adapter Contract 测试

每个新 Adapter 至少验证：

- Factory 能创建子类。
- 未知 options 被拒绝。
- bootstrap 返回唯一 `(source_id, document_id)`。
- 稳定 URI 始终得到相同 document id。
- allowlist 拒绝越权 URL。
- parse 输出非空 `normalized_content` 和 `artifact_content`。
- discover_refs 去重且不会跨来源。
- propose_relative_path 不返回绝对路径或 `..`。
- 来源专属字段只进入 metadata。
- 同一输入重复 parse 得到相同正文 SHA-256。

## 17. 常见问题

### 未知 Adapter

确认实现已经在 `src/doc_sync/adapters/__init__.py` 注册，并检查：

```bash
uv run python -c "from src.doc_sync.adapters import AdapterFactory; print(AdapterFactory.available_types())"
```

### YAML 字段拼写错误

错误会显示字段路径。修正 YAML，不要通过 `extra="ignore"` 绕过。

### URL 不在 allowlist

同时检查 Host、path prefix 和 document URL regex。不要为解决单页问题放宽为任意域名。

### 正文选择器失效

使用 `--limit 10 --dry-run` 验证页面 DOM，再更新 Adapter 默认值和 YAML。

### 文件锁冲突

确认是否存在正常运行的 CronJob。不要删除运行中进程使用的锁文件。

### 大量变化

查看 `large_change` 和候选文档。默认只告警、不阻断；需要人工闸门时设置：

```yaml
policies:
  large_change:
    block_apply: true
```

部分运行还会统计 `failed / (discovered + source_failures)`。失败比例超过
`partial_run.max_failure_ratio` 时，即使 `apply_valid_changes_on_partial_run=true`，
也只生成 manifest/staging 而不写 Markdown，防止限流或解析回退制造错误副本。

### 连续 404

达到阈值后只会标记 `archived_candidate`，不会删除 Markdown。由后续服务或人工审核。

### 路径碰撞

默认追加 `document_id`。若希望严格失败，把 strategy 改为 `fail`。

### 相对路径错误

所有路径先相对于 `workspace_root` 解析；`workspace_root` 自身相对于 YAML 文件解析。

### 数据采集层职责边界

如果问题是“正文为什么没有参数契约”“为什么没有生成 YAML”“为什么没有写入 Zvec”，
应转到后续数据处理层排查。数据爬取模块只保证来源页面、目录层级、Markdown、Hash、
state、manifest 和可恢复写入流程正确；完整流程见 [`data-collection-layer-crawler.md`](./data-collection-layer-crawler.md)。
