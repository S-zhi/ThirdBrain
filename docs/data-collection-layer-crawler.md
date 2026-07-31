# 数据采集层：数据爬取模块设计与运行说明

本文是本项目“数据采集层”的模块级说明，重点解释数据爬取模块从官方文档站发现页面、抓取内容、解析文档结构、生成 Markdown，到按 SHA-256 做增量同步的完整流程。

本文面向需要评审、部署、排障或扩展采集源的开发者和运维人员。它描述的是**来源同步**，不是后续的 API 契约抽取、向量化或检索服务。

## 1. 模块定位

### 1.1 在数据采集层中的位置

```text
数据采集层
└── 数据爬取模块（api-doc-crawler）
    ├── 加载并校验 YAML 配置
    ├── 创建 SourceAdapter
    ├── 发现页面并建立来源注册表
    ├── 抓取/渲染 HTML
    ├── 解析正文、标题、外部 ID 和目录层级
    ├── 规范化为稳定 Markdown
    ├── SHA-256 Hash / Diff 决策
    ├── staging、备份、原子替换
    └── state、manifest、journal 和审计信息

后续数据处理层
├── 最小单元过滤（筛掉目录、概览、纯导航）
├── Markdown → YAML 或 API 文档模型
├── 参数契约/示例/约束抽取
└── Embedding、Zvec 和检索索引
```

数据爬取模块的输出是“可信的、可追踪的 Markdown 副本”，供下游处理。它不把官方 HTML 直接当作检索输入，也不在采集阶段推断 API 参数语义。

### 1.2 负责与不负责

| 范围 | 数据爬取模块负责 | 数据爬取模块不负责 |
|---|---|---|
| 来源 | URL 发现、来源身份、allowlist、robots、重试 | 业务方临时传入任意 URL |
| 内容 | HTML/渲染页面抓取、正文定位、Markdown 规范化 | LLM 改写正文或补齐缺失语义 |
| 结构 | 文档标题、父子目录和稳定文件路径 | 把 API 参数抽成最终契约字段 |
| 更新 | SHA-256、Diff、增加/修改/恢复/移动判断 | 直接覆盖未成功解析的旧文件 |
| 持久化 | Markdown、JSON state、manifest、journal、备份 | MongoDB、Zvec、Embedding |
| 删除 | 统计明确 404/410 的连续缺失 | 因超时、5xx 或未发现就删除文件 |

采集阶段不生成 YAML 文档契约、不调用 LLM、不更新 MongoDB/Zvec。`extract_docs.py` 或后续结构化处理脚本属于数据处理层，应在采集成功后单独运行。

## 2. 总体架构

```text
configs/document_sync.yaml
          │
          ▼
配置加载与预检（路径、来源、数值、凭证字段）
          │
          ▼
AdapterFactory ── Registry ── hiascend / 其他 Adapter
          │
          ▼
SourceAdapter
  bootstrap（扫描既有 Markdown，建立来源注册表）
  initial_refs（配置的 root_urls）
          │
          ▼
发现队列 → URL 规范化/去重/allowlist/max_pages
          │
          ▼
抓取（HTTP 或浏览器渲染）
  robots、超时、QPS、并发、重试、Retry-After、重定向
          │
          ▼
解析与规范化
  article_body → 标题/正文/外部 ID/父级链接/目录层级 → Markdown
          │
          ▼
通用核心
  content_hash + file_hash → Diff → staging → backup → os.replace
          │
          ├── API参考/**/*.md
          ├── state/<source_id>.json
          ├── runs/<run_id>/manifest.json
          ├── journals/<run_id>.json
          └── latest.json
```

核心同步服务只依赖通用协议。它不会根据 `adapter_type` 写 `if hiascend` 分支；官网差异由 Adapter 的发现、解析和路径策略处理。

## 3. Adapter 继承和工厂

### 3.1 三层继承关系

```text
SourceAdapter                 # 所有来源的最小协议
└── HttpDocumentSourceAdapter # HTTP 公共能力
    └── HiascendSourceAdapter # 昇腾页面规则
```

`SourceAdapter` 的稳定协议包括：

| 方法 | 作用 |
|---|---|
| `bootstrap(target_directory)` | 从现有 Markdown 建立来源注册表和旧路径映射 |
| `initial_refs()` | 返回 YAML 中配置的初始发现入口 |
| `fetch(ref, context)` | 获取页面，返回 `FetchResult` |
| `parse(ref, result)` | 把抓取结果解析为 `ParsedDocument` |
| `discover_refs(document)` | 从当前文档发现同版本的更多页面 |
| `propose_relative_path(document)` | 为新身份建议安全的相对路径 |

`HttpDocumentSourceAdapter` 统一提供 HTTP 客户端、URL 规范化、robots、并发、QPS、超时、429/5xx 重试、`Retry-After`、重定向和响应大小限制。非 HTTP 来源（例如 Git 仓库）可以直接继承 `SourceAdapter`。

### 3.2 Factory 与 Registry

```python
AdapterFactory.register(HiascendSourceAdapter)
adapter = AdapterFactory.create(source_config)
```

工厂行为如下：

1. 读取 YAML 的 `adapter.type`。
2. 在显式 Registry 中查找实现。
3. 使用实现声明的 `config_model` 校验 `options`。
4. 未知类型直接失败，并列出可用类型。
5. 重复注册同一 `adapter_type` 直接失败。
6. 不允许从 YAML 动态导入任意 Python 类路径。

新增来源只需要新增 Adapter 配置模型、继承合适的基类、注册并编写 Contract 测试，不需要修改通用同步服务。

## 4. 通用数据模型

来源专属字段不能污染核心模型顶层；例如昇腾的 `node_id`、`page_id`、`version`、`product` 只能放在 `metadata`。所有 Pydantic 模型均使用 `extra="forbid"`，拼写错误会在启动时失败。

### 4.1 `DocumentRef`：发现队列中的来源引用

```text
source_id             来源配置 ID
document_id           稳定文档身份；不是文件名，也不是 URL
canonical_uri         规范化 URI
parent_document_id    发现该页面的父文档，可为空
title_hint            发现阶段得到的标题提示
metadata              可 JSON 序列化的来源专属字段
```

稳定身份使用 `(source_id, document_id)`。URL 改变但身份不变时，记录为 `moved`，不生成重复文档。

### 4.2 `FetchResult`：抓取证据

```text
requested_uri         请求的规范 URI
final_uri             最终 URI（需通过重定向策略）
status_code            HTTP 状态码
content_type           响应类型
body                  原始或渲染后的字节
fetched_at             抓取时间
response_hash          原始响应 SHA-256，用于排障
metadata              抓取器信息，例如是否使用浏览器
```

`response_hash` 不是更新判定依据。网页可能只改变广告、脚本或空白，只有解析后的规范正文才决定 Markdown 是否变化。

### 4.3 `ParsedDocument`：解析后的来源文档

```text
source_id
document_id
canonical_uri
external_id            来源显示的节点/页面 ID
title
hierarchy              从大标题到小标题的目录段列表
normalized_content     用于 Hash/Diff 的规范正文
artifact_content       实际写入 Markdown 的内容
discovered_refs        从正文或父级链接发现的后续页面
metadata               来源专属元数据
```

`normalized_content` 和 `artifact_content` 的职责不同：前者要尽量稳定，避免官网无意义排版变化导致更新；后者保留阅读所需的标题、来源、节点和正文包装。

### 4.4 `SyncStateEntry` 与 manifest

状态至少记录 `content_hash`、`file_hash`、`relative_path`、`canonical_uri`、`last_seen_at`、`missing_count` 和生命周期状态。状态按 source 分文件保存，避免多个来源互相污染。

manifest 是一次运行的审计快照，包含运行状态、统计、每个文档的操作、旧/新 Hash、路径、错误和来源元数据。它不是下一次运行唯一的数据源，下一次 Diff 以 state 加当前文件为准。

## 5. 端到端流程

### 5.1 阶段一：配置加载和启动预检

CLI 默认读取 `configs/document_sync.yaml`，路径规则是：

1. `workspace_root` 相对于 YAML 文件所在目录解析。
2. 其他相对路径（目标目录、运行目录、浏览器缓存目录）相对于 `workspace_root` 解析。
3. 不依赖执行命令时的当前目录。

启动前一次性检查：

- `schema_version` 是否支持。
- source ID 是否唯一。
- Adapter 类型是否已注册。
- Adapter options 是否通过强类型校验。
- runtime 与 target 目录是否互相包含。
- 不同 source 的目标目录是否重叠。
- 并发、QPS、超时、重试、响应大小是否为合法正数。
- root URL 是否在 Host、路径前缀和文件名正则的 allowlist 内。
- YAML 是否出现密码、Token、Cookie 等明文凭证字段。

预检失败时不访问官网、不修改 Markdown、不更新 state。

### 5.2 阶段二：bootstrap 既有文档注册表

首次运行不是把官网页面盲目写入空目录，而是先扫描已有 Markdown。Hiascend Adapter 从每个文件的来源行、节点行和相对路径恢复：

```text
(source_id, document_id) → canonical_uri → 已存在 relative_path
```

这一步的作用：

- 让已有 2249 个页面成为可比对的稳定基线。
- 同一个节点在 URL 改变时沿用原文件，不产生副本。
- 让旧目录层级成为新页面路径建议的优先依据。
- 发现同一身份重复出现时，保留更完整、更深的目录路径并报告冲突。

推荐先做小范围和全量 dry-run，再执行 `bootstrap --apply` 建立 state。

### 5.3 阶段三：初始入口和页面发现

`initial_refs()` 从 YAML 的 `root_urls` 建立种子。每个解析成功的页面再由 `discover_refs()` 返回后续引用，形成有边界的发现图。

发现过程执行：

1. URL 规范化（scheme、host、路径和片段统一）。
2. 按 canonical URI 去重。
3. 检查 allowed host、allowed path prefix、document URL pattern。
4. 拒绝跨版本、跨产品或不符合页面模式的链接。
5. 限制 `max_pages`，超过上限的链接不进入本次运行。
6. 记录父文档关系，便于目录层级和排障。

链接发现是“候选发现”，不是“写文件承诺”；只有抓取、解析和路径安全检查都通过后才进入 staging。

### 5.4 阶段四：抓取与渲染

HTTP Adapter 走共享 HTTP 能力：robots 检查、并发/QPS 限制、超时、429 `Retry-After`、5xx 指数退避、最大响应大小和同域重定向检查。

Hiascend 当前配置支持浏览器渲染：

```yaml
browser:
  channel: chrome
  headless: true
  wait_until: domcontentloaded
  navigation_timeout_ms: 60000
  selector_timeout_ms: 60000
  rendered_html_directory: ./data/doc_sync/rendered_html
```

当正文依赖 JavaScript 时，渲染后的 HTML 才是解析输入。渲染缓存只用于排障和复现，不能替代 state，也不能直接当作最终 Markdown。

浏览器启动、页面关闭或响应超时只影响当前文档：旧文件保留，manifest 记录 `failed`。当前 Hiascend Adapter 在一个 source 生命周期内复用 Playwright 浏览器和上下文、每页只新建并关闭 Page。生产 Cron 上线前仍必须用目标机器做容量测试；如果日志显示每页重新启动 Chrome，应视为生命周期回归，优先恢复进程/上下文复用并保留并发上限。

实现边界要特别注意：`HttpDocumentSourceAdapter` 的默认 `fetch` 会调用共享
`HttpFetchClient`，但当前 Hiascend 的动态浏览器 `fetch` 为满足 JavaScript 渲染而覆盖了
该方法，实际只在 Adapter 内执行 allowlist、浏览器超时和页面生命周期控制，不自动获得
HTTP 客户端的 robots、QPS、429/5xx 重试和响应大小闸门。生产环境若要求这些策略对浏览器
请求同样生效，应在浏览器路径增加同等的限速/robots/重试封装，并在上线验收中单独验证；
文档中的 HTTP 公共能力不能被理解为浏览器路径已经全部继承。

### 5.5 阶段五：HTML 解析和 Markdown 规范化

Hiascend Adapter 的解析顺序如下：

1. 在 `selectors.article_body`（默认 `.the-article-body`）定位正文容器。
2. 按 `selectors.title`（默认 `h1`、`.topictitle1`）提取标题。
3. 从页面、现有 Markdown 或稳定 URL 规则解析 `document_id` / `external_id`。
4. 清理 `script`、`style`、无关导航、展开控件和重复页脚。
5. 将标题、段落、列表、代码块、表格、链接和图片转换成稳定 Markdown。
6. 规范化 Unicode、换行、空白和尾随空格，避免同一正文因 HTML 排版差异产生不同 Hash。
7. 生成 `normalized_content` 和最终 `artifact_content`。
8. 从正文的父级链接、标题和现有路径推断 `hierarchy`。
9. 解析正文中的同版本链接，生成 `discovered_refs`。

正文缺失、标题为空、文档 ID 无法确定或规范内容为空时，解析失败，不覆盖旧文件。

### 5.6 阶段六：目录层级识别与路径生成

目录层级是本模块的关键语义，不能把页面标题和正文全部平铺到一个目录。Hiascend 的优先级是：

1. **已有文档路径优先**：同一 `document_id` 已存在时沿用原有目录。
2. **页面标题回溯**：从页面 `<title>` 的“子级 - 中级 - 大级 - 文档标题”形式识别目录段，并反转为“大级/中级/子级”。
3. **父级链接补充**：使用 `.familylinks a` 等配置选择器发现父页面。
4. **无法判断时隔离**：放入配置的 `_待归类`，而不是猜一个可能错误的目录。

例如官网页面表达的是：

```text
SIMD API → C API → Memory 矢量计算 → Exp
```

最终路径应保持为：

```text
API参考/SIMD_API/C_API/Memory矢量计算/Exp.md
```

目录段会做路径安全清洗：拒绝绝对路径、`..`、空段和控制字符。若同一目录下已有不同身份占用相同文件名，默认按 `path_collision.strategy: append_document_id` 追加稳定 ID；设置为 `fail` 时直接标记错误。

解析层级时不会把当前页面的标题重复追加到父级层级。页面 `<title>` 中识别到的 child-to-parent
目录段会反转为 parent-to-child；父级链接则按选择器返回的 DOM 顺序去重。因此新增来源时，
必须用真实页面 fixture 验证父链顺序，不能假设所有站点的导航方向相同。旧版平铺问题的根因
通常是只取最终页面标题、没有消费官网父子导航，或把 child-to-parent 顺序当成
parent-to-child。当前规则先使用已有路径，再使用标题和父链，并在 Adapter Contract 中固定方向。

### 5.7 阶段七：Hash、Diff 与状态转移

三种 Hash 的含义：

| Hash | 输入 | 用途 |
|---|---|---|
| `response_hash` | 原始/渲染响应字节 | 排查抓取差异，不决定是否写文件 |
| `content_hash` | `normalized_content` | 判断官网正文是否变化 |
| `file_hash` | 磁盘上 Markdown 字节 | 判断本地文件是否被人工修改 |

通用决策：

| 操作 | 判定 | 行为 |
|---|---|---|
| `added` | 稳定身份不存在 | 创建新 Markdown |
| `unchanged` | 来源 `content_hash` 和本地 `file_hash` 都未变化 | 不写文件、不改变 mtime |
| `updated` | 来源 `content_hash` 变化 | staging 后原子覆盖 |
| `restored` | 来源未变但本地文件被修改 | 按策略恢复来源版本 |
| `moved` | 身份相同但 canonical URI 变化 | 更新来源 URI，避免重复文件 |
| `failed` | 抓取、解析、校验或路径失败 | 保留旧文件 |
| `missing` | 明确 404/410 | 增加 `missing_count` |
| `archived_candidate` | 明确缺失连续达到阈值 | 保留文件，仅标记候选归档 |

超时、429、5xx、robots 拒绝和未从本次发现图出现，都不是删除证据。

### 5.8 阶段八：staging、备份、原子应用

所有候选内容先写入：

```text
data/doc_sync/staging/<run_id>/
```

应用时：

1. 校验相对路径在目标目录内。
2. 校验正文非空、Hash 与候选一致。
3. 对旧文件写入 `backups/<run_id>/`。
4. 在目标文件同目录创建临时文件。
5. 使用 `os.replace()` 原子替换，避免读者看到半个文件。
6. 每完成一个动作刷新 `journals/<run_id>.json`。
7. 全部动作成功后更新 source state 和 `latest.json`。
8. 生成最终 manifest。

单页失败不会回滚其他已经成功的有效页面；中断时按 journal 恢复未完成动作。`--dry-run` 只生成候选和 manifest，不修改 Markdown 或 state。

### 5.9 阶段九：state、manifest 和可观测性

运行目录：

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

manifest 至少包含：

- `run_id`、`mode`、`trigger`、开始/结束时间和 `status`。
- `stats.discovered/added/updated/restored/unchanged/failed/archived_candidates`。
- `updated_markdown`：本轮真实写入的 Markdown 相对路径。
- `source_results`：每个 source 的状态和统计。
- `documents`：每个身份的操作、路径、旧/新 Hash、缺失次数和 metadata。
- `errors`：错误类型、页面和可定位信息。

查看“这次哪些文档发生了变化”，优先看：

```bash
jq '.updated_markdown' data/doc_sync/latest.json
jq '.documents[] | select(.operation != "unchanged") |
  {operation, relative_path, document_id, old_content_hash, new_content_hash, reason}' \
  data/doc_sync/latest.json
```

`updated_markdown` 为空表示没有文件真正写入；manifest 中仍可能有 `failed`、`missing` 或 dry-run 候选。要判断本地被谁改过，比较 `old_file_hash`、`new_file_hash` 和 state 中的 `file_hash`。

## 6. 配置说明和常用操作

配置文件为 `configs/document_sync.yaml`，初始化字段和默认值详见 [`document-sync-setup.md`](./document-sync-setup.md)。常用命令：

```bash
# 只校验配置和 Registry，不访问官网
uv run python -c "from src.doc_sync.config import load_document_sync_config; from src.doc_sync.adapters import AdapterFactory; c=load_document_sync_config('configs/document_sync.yaml'); AdapterFactory.validate_sources(c.sources); print(AdapterFactory.available_types())"

# 首次少量检查
uv run python -m src.script.sync_docs bootstrap --dry-run --limit 10

# 首次全量预览，不修改 Markdown
uv run python -m src.script.sync_docs bootstrap --dry-run

# 审核 manifest 后建立状态并应用
uv run python -m src.script.sync_docs bootstrap --apply

# 日常同步
uv run python -m src.script.sync_docs sync --apply --trigger scheduled

# 只运行一个 source
uv run python -m src.script.sync_docs sync --source hiascend-cann-910beta3 --dry-run

# 从 journal 恢复中断的应用阶段
uv run python -m src.script.sync_docs resume --run-id <run_id>
```

如果本机 `uv` 缓存目录没有写权限，可使用项目虚拟环境中的解释器运行同一个模块；生产部署应修复运行用户和缓存目录权限，而不是绕过配置校验。

CLI 退出码：

| 退出码 | 含义 |
|---:|---|
| `0` | 全部成功或无变化 |
| `1` | 配置、系统或无法继续的失败 |
| `2` | 部分页面失败，但有效页面已按策略处理 |
| `3` | 文件锁冲突 |

## 7. 失败、恢复和安全边界

| 现象 | 判断 | 处理 |
|---|---|---|
| 未知 Adapter | Registry 没有 `adapter.type` | 检查显式注册和可用类型 |
| YAML 字段拼写错误 | `extra="forbid"` 校验失败 | 修正字段，不要放宽为 ignore |
| URL 不在 allowlist | Host、路径或正则不匹配 | 修正精确白名单，不允许任意域名 |
| 正文选择器失效 | article body 为空或正文过短 | 用少量 dry-run 检查渲染 HTML 后再改选择器 |
| Chrome 启动/关闭 | 当前页抓取失败 | 保留旧文件；检查浏览器依赖、进程复用和容量 |
| 429/5xx/超时 | 暂时性网络错误 | 有限重试；最终只标记该页 failed |
| 文件锁冲突 | 另一个 Cron 或人工任务运行 | 等待任务结束，不删除活动锁 |
| 大量变化 | `large_change=true` | 审核候选；需要人工闸门时设置 `block_apply: true` |
| 连续 404/410 | 可能下线或路径迁移 | 达阈值后只标记候选归档，人工确认后再处理 |
| 路径碰撞 | 两个身份建议相同路径 | 默认追加 document ID，或切换为 fail |
| 中断 | journal 中有未完成动作 | `resume --run-id`；staging 被清理后重新 dry-run |
| 旧扁平副本 | 历史运行生成错误路径 | 移入 `data/doc_sync/quarantine/<run_id>/`，保留 quarantine manifest，可恢复 |

安全约束：

- 只访问 YAML allowlist 中的 Host 和路径。
- 遵守 robots.txt、QPS、并发和最大响应大小。
- 不在 YAML、日志、state 或 manifest 中保存密码、Token、Cookie；凭证只允许引用环境变量名。
- 所有输出路径必须位于 source 目标目录内，拒绝绝对路径和 `..`。
- 运行用户只需要目标 Markdown 和 `data/doc_sync` 的读写权限，不需要仓库之外的广泛写权限。

## 8. 新增 Adapter 的开发流程

1. 定义来源专属 Pydantic options，设置 `extra="forbid"`。
2. HTTP 网站继承 `HttpDocumentSourceAdapter`，其他来源继承 `SourceAdapter`。
3. 实现 bootstrap、initial_refs、fetch、parse、discover_refs 和路径建议。
4. 把页面 ID、版本、产品等信息放进 `metadata`，不修改通用模型。
5. 声明唯一 `adapter_type` 和 `config_model`。
6. 在 `src/doc_sync/adapters/__init__.py` 显式注册。
7. 在 YAML 的 `sources` 中添加配置。
8. 通过 Adapter Contract 测试，再用 10 页 dry-run 验证正文和层级。
9. 审核 manifest 的 `relative_path`、Hash、错误和大变化比例后再 apply。

Contract 测试至少覆盖：工厂创建、未知字段失败、稳定 ID、allowlist、非空规范正文、发现去重、安全相对路径、metadata 隔离和重复解析得到相同 SHA-256。

## 9. 上线验收清单

### 配置和权限

- [ ] `document_sync.yaml` 可以从任意当前目录加载。
- [ ] source ID 唯一，目标目录不重叠，runtime 不越界。
- [ ] 没有明文密码、Token、Cookie。
- [ ] Cron/Kubernetes 使用持久化的 Markdown、state、runs 和 backups。

### 解析和目录

- [ ] `bootstrap --dry-run` 不修改 Markdown 和 state。
- [ ] 现有文档能按来源 URL、外部 ID 和目录建立唯一注册表。
- [ ] `SIMD API/C API/Memory 矢量计算/具体文档` 等层级不会被平铺。
- [ ] 无法分类的页面进入 `_待归类`，不静默丢失。
- [ ] Markdown 正文选择器、标题、来源和节点行均可复现。

### 增量和恢复

- [ ] 第二次无变化运行的 `updated_markdown` 为空且 mtime 不变。
- [ ] 新增/修改/恢复/移动/失败/缺失在 manifest 中可区分。
- [ ] 中断后 journal 可以 resume，旧文件始终可从 backup 恢复。
- [ ] 旧错误副本在 quarantine 中可追溯、可恢复，不直接删除。

### 生产容量

- [ ] 在目标服务器完成浏览器依赖和并发容量测试。
- [ ] 全量运行时间小于 Cron 窗口，并设置外部 job 超时。
- [ ] 浏览器模式优先复用进程/上下文，避免每页启动 Chrome。
- [ ] 失败退出码、manifest 和日志已接入告警。

## 10. 相关文件

| 文件 | 作用 |
|---|---|
| `configs/document_sync.yaml` | 独立同步配置 |
| `src/script/sync_docs.py` | bootstrap、sync、resume CLI |
| `src/doc_sync/models.py` | 通用文档、状态和 manifest 模型 |
| `src/doc_sync/adapters/factory.py` | Adapter Registry 与工厂 |
| `src/doc_sync/adapters/hiascend.py` | 昇腾发现、浏览器抓取、解析和层级规则 |
| `src/doc_sync/service.py` | Hash、Diff、路径安全、原子写入和状态 |
| `docs/document-sync-setup.md` | 初始化、字段和日常运维 |
| `Skill/pipeline/api-doc-crawler/SKILL.md` | 数据爬取模块的执行契约 |
| `Skill/PIPELINE.md` | 采集、筛选、结构化处理的流水线关系 |
