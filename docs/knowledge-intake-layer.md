# Knowledge Intake Layer 技术方案

> 状态：**已拍板，可实施**
> 目标版本：Phase 0 + Phase 1
> 面向读者：负责实现、评审、测试和验收的代码 Agent / 工程师
> 最后更新：2026-08-02

## 1. 执行摘要

本方案在 Gateway 与现有 Knowledge 写入面之间增加一层独立的 **Knowledge Intake**：
接收调用方提交的原始 Markdown，经过确定性的规范化、解析、分块、稳定标识和 Scope
绑定后，构造现有 `WikiUpdateInput`，再调用
`KnowledgeUpdateService.update_wiki()` 完成 LLM 编译、证据校验、保守合并、staging、发布和
派生索引更新。

最终链路如下：

```mermaid
flowchart LR
    C["调用方"] -->|"原始 Markdown + 明确 Scope"| G["POST /api/v1/knowledge/intake"]
    G --> I["KnowledgeIntakeService"]
    I --> N["规范化"]
    N --> P["Markdown AST 解析"]
    P --> S["确定性分块与稳定 part_id"]
    S --> B["WikiUpdateInputBuilder"]
    B --> U["KnowledgeUpdateService.update_wiki()"]
    U --> E["OpenAIKnowledgeExtractor：Artifact / Claim / Evidence"]
    E --> V["确定性验证与保守合并"]
    V --> M["staging → catalog publish → Zvec index"]
```

本方案最重要的边界是：

1. Intake 层不提取 Claim，不生成 Evidence，不做知识合并。
2. Markdown 分块、哈希、Part ID 和拓扑全部由确定性代码完成，不信任 LLM 输出。
3. `wiki_id`、`namespace`、`version` 和 `document_id` 必须由调用方显式提供；发布路径不猜
   Scope。
4. `KnowledgeUpdateService.update_wiki()` 的签名、staging/catalog 原子发布语义、Wiki 隔离
   语义保持不变。
5. 查询侧五阶段 Trace `trigger → recall → rerank → inject → generate` 保持不变；Intake
   使用独立 Trace，不插入所谓第六阶段。
6. Phase 1 只发生一次 LLM 调用：现有 `OpenAIKnowledgeExtractor` 的知识编译调用。

## 2. 背景与现状

### 2.1 当前写入契约

现有结构化入口是：

```text
POST /api/v1/knowledge/update
  → KnowledgeUpdateRequest
  → WikiUpdateInput
  → KnowledgeUpdateService.update_wiki()
```

`WikiUpdateInput` 只描述 Wiki、文档、Scope 和 Source Parts，不包含 Claim 或 Evidence。
`OpenAIKnowledgeExtractor` 才负责把 `KnowledgeDocumentInput` 编译成带 Claim/Evidence 的
`ArtifactDraft`。因此 Intake 层只能产出 `WikiUpdateInput`，不能提前提取一遍 Claim 后又让
现有 Extractor 重复提取。

当前 Source Part 的重要语义：

- `SourcePart.content_hash` 是 `content` 的派生属性，由服务端计算，不是输入字段。
- Evidence 中的 `char_start` / `char_end` 相对 `SourcePart.content`，不是相对整篇 Markdown。
- 写入面不会重新切分或重排调用方提供的 Parts。
- `KnowledgeUpdateService` 以 `document.content_hash + compiler_fingerprint` 判断是否需要重新
  编译。

### 2.2 当前发布语义

现有写入服务按文档隔离失败：

- 单文档的 Source Revision 和对应 Artifact Revisions 是同一发布单元。
- 校验通过后先写 staging，再原子切换 Wiki catalog 指针。
- 查询只读取 catalog 可达 revision，不可见半发布状态。
- 索引是可重建派生物，索引更新失败不回滚已经发布的正式知识。
- `update_wiki()` 内部扁平化 `WikiUpdateInput.documents` 后复用 `update_knowledge()`。

这些语义均不由 Intake 层复制或替代。

### 2.3 已确认的现存契约问题

实现 Phase 1 前必须先完成 Phase 0，处理两个现存问题。

#### P0-1：独立 Wiki 的空 collection 契约不一致

`KnowledgeDocumentInput.rag_collection_id` 和 `RagCollectionInput.rag_collection_id` 允许空字符串，
表示文档直接进入独立 Wiki；但 `EvidenceRef.rag_collection_id` 当前要求 `min_length=1`。
真实 OpenAI Extractor 若按 Source 原样返回空字符串，`ExtractionResult.model_validate()` 会失败。

修复原则：

- 允许 `EvidenceRef.rag_collection_id == ""`。
- 仍要求 Evidence 的值与当前 Document 的值严格相等；空只代表“没有历史 collection 身份”，
  不是跳过来源校验。
- 不允许 Intake 层伪造 `direct-intake` 等 collection ID，因为这会改变 Source 的稳定身份。
- 这是兼容性扩展，不需要 Mongo 数据迁移；已有非空值继续合法。

GitNexus 对 `EvidenceRef` 的当前上游影响评估为 **CRITICAL**：55 个受影响符号、32 个直接
依赖，且结果是 lower-bound。实现 Agent 在修改前必须重新执行 impact，向用户报告风险，
并在修改后跑完整 Knowledge 单测。

#### P0-2：Extractor 身份与 `UpdateOptions` 默认值不一致

当前运行时默认 Extractor 身份为：

```text
extractor_version = openai-compatible-v1
prompt_version    = knowledge-compiler-v1
model             = 运行时 KNOWLEDGE_LLM_MODEL / OPENAI_MODEL
```

但 `UpdateOptions` 默认值为 `v1 / v1 / model-v1`。如果 raw intake 直接使用
`UpdateOptions()`，现有 `_validate_extraction_metadata()` 会把真实 Extractor 结果判为
`EXTRACTOR_METADATA_MISMATCH`。

拍板方案：

- Intake API 不允许调用方声明 `extractor_version`、`prompt_version`、`model` 或
  `schema_version`。
- 编译器身份由组合根根据实际装配的 Extractor 注入 `KnowledgeIntakeService`。
- 调用方只能传 `actor`、`force_reprocess`、`update_indexes` 三个运行选项。
- Intake Service 将运行选项覆盖到服务端持有的基础 `UpdateOptions` 上，再调用
  `update_wiki()`。
- 结构化专家入口 `/knowledge/update` 本期保持兼容，不在本功能中重构其公开契约。

## 3. 目标与非目标

### 3.1 Phase 1 目标

- 新增同步、单文档 Markdown Intake API。
- 调用方只负责提供原始 Markdown、稳定文档身份和明确 Scope。
- 生成稳定、可追溯、严格有序的 `SourcePart` 拓扑。
- 服务端计算整文档 hash；Part hash 继续使用现有派生属性。
- 转换为现有 `WikiUpdateInput` 并委托给唯一发布入口 `update_wiki()`。
- 保持一次请求最多一次知识编译 LLM 调用；重复内容命中 unchanged 时为零次。
- 返回机器可消费的准备报告、更新结果和 Intake Trace。
- 所有新行为有单测，核心闭环有使用 Fake Extractor 的端到端测试。

### 3.2 明确非目标

- 不支持 YAML、HTML 专用解析器。
- 不抓取 URL，不访问 `source_url`。
- 不批量处理多个文档。
- 不引入任务队列或异步作业状态机。
- 不推断或改写 `wiki_id`、`namespace`、`version`。
- 不让 Intake 层生成 Claim、Evidence、Artifact 或合并建议。
- 不改变 Query Trace 的五阶段模型。
- 不自动修复多义 Evidence 引用。
- 不实现跨 Wiki 推理或跨 Scope 关系。
- 不改 `KnowledgeUpdateService.update_wiki()` 签名。
- 不把新解析器塞进 `src/knowledge/` 核心领域模块。

## 4. 已拍板的架构决策

| ID | 决策 | 结论 |
|---|---|---|
| D1 | Phase 1 输入 | 只接受 Markdown 字符串，不接受文件路径、URL、YAML、HTML 专用输入 |
| D2 | 模块位置 | 新建 `src/knowledge_intake/`，作为 Knowledge 写入面的外围适配层 |
| D3 | Gateway | 新建 `POST /api/v1/knowledge/intake`，保留 `/knowledge/update` 不变 |
| D4 | Scope | `wiki_id + document_id + namespace + version` 全部必传、原样保留、发布路径不推断 |
| D5 | 语义提取 | Intake 不调用 LLM；Claim/Evidence 只由现有 `KnowledgeExtractor` 生成一次 |
| D6 | 确定性数据 | 规范化、分块、Part ID、拓扑、hash 全部由服务端计算 |
| D7 | Trace | 新建 Intake Trace；不修改查询 `TraceStage` 和五阶段顺序 |
| D8 | 失败语义 | 准备阶段失败不调用写入服务；进入现有更新流程后沿用 `UpdateResult` 状态语义 |
| D9 | Expert 预览 | Phase 2 使用 `/knowledge/intake/preview`，不暴露实现名 `/agent-understand` |
| D10 | 缓存失效 | Intake 输入 schema/分块算法版本进入服务端 `UpdateOptions.schema_version` |

## 5. 目标模块与文件布局

### 5.1 新增模块

```text
src/knowledge_intake/
├── __init__.py
├── constants.py          # 版本号、限制默认值、稳定错误码
├── contracts.py          # Parser / Partitioner / Observer 等 Protocol
├── models.py             # Raw/Prepared/Report/Trace 内部模型
├── normalization.py      # Markdown 规范化与行偏移表
├── markdown_parser.py    # markdown-it-py token 适配
├── partitioner.py        # section/atomic block 分组与稳定 part_id
├── builder.py            # PreparedDocument → WikiUpdateInput
└── service.py            # Intake 总编排并调用 update_wiki()
```

### 5.2 Gateway 新增文件

```text
src/gateway/knowledge_intake_schemas.py
src/gateway/knowledge_intake_router.py
```

### 5.3 需要修改的现有文件

| 文件 | 修改目的 |
|---|---|
| `src/knowledge/models.py` | Phase 0：让 `EvidenceRef.rag_collection_id` 与独立 Wiki 契约一致 |
| `src/gateway/__init__.py` | 导出 `knowledge_intake_router` |
| `src/main.py` | 装配 Intake Service、保存到 app state、include router |
| `pyproject.toml` / `uv.lock` | 增加 Markdown AST 解析依赖 |
| `docs/knowledge-update-plane.md` | 实现完成后补充 raw intake 与 structured update 的区别 |

### 5.4 明确禁止修改

Phase 1 不应修改以下语义：

- `KnowledgeUpdateService.update_wiki()` 方法签名。
- `KnowledgeRepository.stage()` / `publish()` / `abandon()` 协议。
- `WikiUpdateInput` 字段结构。
- `SourcePart.content_hash` 派生逻辑。
- `OpenAIKnowledgeExtractor` 的 Claim/Artifact 职责。
- `TraceStage` 查询模型。
- Mongo staging、revision、catalog 的可见性模型。

## 6. API 契约

### 6.1 Endpoint

```http
POST /api/v1/knowledge/intake
Authorization: Bearer <KNOWLEDGE_API_KEY>
Content-Type: application/json
```

路由必须复用 `require_service_auth`，不得新增另一套鉴权配置。

### 6.2 请求模型

建议在 `knowledge_intake_schemas.py` 中定义：

```python
class KnowledgeIntakePublishOptions(GatewayModel):
    actor: str = Field(default="system", min_length=1, max_length=256)
    force_reprocess: bool = False
    update_indexes: bool = True


class KnowledgeIntakeRequest(GatewayModel):
    wiki_id: str = Field(min_length=1, max_length=256)
    document_id: str = Field(min_length=1, max_length=256)
    namespace: str = Field(min_length=1, max_length=512)
    version: str = Field(min_length=1, max_length=128)
    markdown: str = Field(min_length=1)
    rag_collection_id: str = Field(default="", max_length=512)
    source_path: str | None = Field(default=None, max_length=4096)
    source_url: str | None = Field(default=None, max_length=4096)
    source_metadata: dict[str, object] = Field(default_factory=dict)
    metadata: dict[str, object] = Field(default_factory=dict)
    publish: KnowledgeIntakePublishOptions = Field(
        default_factory=KnowledgeIntakePublishOptions
    )
```

补充校验规则：

- `wiki_id`、`document_id`、`namespace`、`version` 可在校验时检查首尾空白，但不得自动
  小写化、大小写折叠或改写官方字符串。
- `markdown` 不使用 `strip_whitespace=True`，避免改变原文；单独检查其是否至少包含一个
  非空白字符。
- `source_url` 在 Phase 1 只是 provenance 字符串，不发起网络请求。
- `source_metadata` 和 `metadata` 必须限制序列化后的总大小，默认各不超过 16 KiB。
- API 不接受 `content_hash`、Parts、Extractor 身份或 compiler fingerprint；这些由服务端
  生成。

示例：

```json
{
  "wiki_id": "wiki:ascendc",
  "document_id": "asc-reduce-max",
  "namespace": "AscendC.910beta3",
  "version": "910beta3",
  "markdown": "# asc_reduce_max\n\n`asc_reduce_max` reduces values.\n",
  "source_path": "docs/asc_reduce_max.md",
  "source_url": "https://example.invalid/docs/asc_reduce_max",
  "source_metadata": {
    "repository": "official-api-docs",
    "revision": "2026-08-01"
  },
  "publish": {
    "actor": "docs-sync",
    "force_reprocess": false,
    "update_indexes": true
  }
}
```

### 6.3 响应模型

```python
class IntakeStageResult(GatewayModel):
    name: str
    status: str
    duration_ms: int = Field(ge=0)
    details: dict[str, object] = Field(default_factory=dict)


class IntakePreparationReport(GatewayModel):
    normalization_version: str
    parser_version: str
    partitioner_version: str
    intake_schema_version: str
    content_hash: str
    original_bytes: int = Field(ge=0)
    normalized_chars: int = Field(ge=0)
    part_count: int = Field(ge=1)
    warnings: tuple[str, ...] = ()


class KnowledgeIntakeResponse(GatewayModel):
    request_id: str
    preparation: IntakePreparationReport
    update: UpdateResult
    trace: tuple[IntakeStageResult, ...]
```

示例：

```json
{
  "request_id": "b591f320-8fde-4f44-bb85-b49e305b9ad2",
  "preparation": {
    "normalization_version": "markdown-normalizer-v1",
    "parser_version": "markdown-it-commonmark-v1",
    "partitioner_version": "knowledge-partitioner-v1",
    "intake_schema_version": "knowledge-1.md-intake-1",
    "content_hash": "9e9f...64-hex-characters",
    "original_bytes": 1824,
    "normalized_chars": 1769,
    "part_count": 4,
    "warnings": []
  },
  "update": {
    "operation_id": "5b6d...",
    "wiki_id": "wiki:ascendc",
    "status": "completed",
    "documents_received": 1,
    "documents_created": 1,
    "documents_updated": 0,
    "documents_unchanged": 0,
    "documents_failed": 0,
    "validation": {
      "passed": true,
      "issues": [],
      "warnings": []
    },
    "provenance_coverage": 1.0,
    "outcomes": []
  },
  "trace": [
    {"name": "normalize", "status": "completed", "duration_ms": 1, "details": {}},
    {"name": "parse", "status": "completed", "duration_ms": 2, "details": {}},
    {"name": "partition", "status": "completed", "duration_ms": 1, "details": {"parts": 4}},
    {"name": "build", "status": "completed", "duration_ms": 0, "details": {}},
    {"name": "update", "status": "completed", "duration_ms": 431, "details": {}}
  ]
}
```

响应中不得返回完整 Markdown、完整 Prompt、密钥、provider 原始异常或数据库内部错误。

### 6.4 HTTP 与领域失败语义

| HTTP | 稳定错误码/结果 | 说明 |
|---|---|---|
| 200 | `KnowledgeIntakeResponse` | 准备成功且 `update_wiki()` 返回；内部 status 可以是 completed/partial/failed |
| 401 | FastAPI 鉴权错误 | API key 缺失或错误 |
| 413 | `KNOWLEDGE_INTAKE_TOO_LARGE` | 整份 Markdown 或 metadata 超过请求级硬限制 |
| 422 | FastAPI validation | 请求字段缺失、类型错误、空 Scope 等 |
| 422 | `KNOWLEDGE_INTAKE_INVALID_MARKDOWN` | 解析器无法产生合法 token/section 结构 |
| 422 | `KNOWLEDGE_INTAKE_TOO_MANY_PARTS` | 分块结果超过 Part 数限制 |
| 422 | `KNOWLEDGE_INTAKE_ATOMIC_BLOCK_TOO_LARGE` | 不允许拆分的代码块、表格或 HTML block 过大 |
| 422 | `KNOWLEDGE_INTAKE_PREPARATION_FAILED` | 拓扑或构造后的领域模型校验失败 |
| 503 | `KNOWLEDGE_INTAKE_DISABLED` | Knowledge Update Service/LLM provider 未装配 |
| 503 | `KNOWLEDGE_INTAKE_UNAVAILABLE` | Intake 层未预期的运行期故障，响应必须脱敏 |

一旦 `update_wiki()` 已被调用，应完整返回现有 `UpdateResult`，不把其中的 per-document
`failed/partial` 再翻译成 422。这样 raw 与 structured 写入共享相同领域语义。

## 7. Intake 内部模型与依赖方向

### 7.1 内部输入

```python
class RawMarkdownDocument(IntakeModel):
    wiki_id: str
    document_id: str
    namespace: str
    version: str
    markdown: str
    rag_collection_id: str = ""
    source_path: str | None = None
    source_url: str | None = None
    source_metadata: dict[str, object] = Field(default_factory=dict)
    metadata: dict[str, object] = Field(default_factory=dict)
```

Gateway schema 与内部模型分开，避免 FastAPI 传输语义渗入解析核心。

### 7.2 准备结果

```python
class PreparedWikiUpdate(IntakeModel):
    wiki: WikiUpdateInput
    report: IntakePreparationReport
    trace: tuple[IntakeStageResult, ...]
```

### 7.3 Protocol

```python
class MarkdownDocumentParser(Protocol):
    def parse(self, markdown: str) -> ParsedMarkdown: ...


class SourcePartPartitioner(Protocol):
    def partition(
        self,
        *,
        document_id: str,
        normalized_markdown: str,
        parsed: ParsedMarkdown,
    ) -> tuple[SourcePart, ...]: ...


class KnowledgeWikiPublisher(Protocol):
    async def update_wiki(
        self,
        request: WikiUpdateInput,
        options: UpdateOptions | None = None,
    ) -> UpdateResult: ...
```

`KnowledgeUpdateService` 通过结构化类型自然满足 `KnowledgeWikiPublisher`，Intake 模块不需要
反向 import Gateway，也不依赖 Mongo、Zvec 或 OpenAI SDK。

### 7.4 依赖方向

```text
gateway → knowledge_intake → knowledge public models/protocol surface
                              ↓
                       KnowledgeUpdateService
                              ↓
              extractor / repository / index adapters
```

禁止出现：

```text
knowledge → knowledge_intake
knowledge_intake → gateway
knowledge_intake → Mongo/OpenAI/Zvec concrete classes
```

## 8. Markdown 规范化规范

### 8.1 规范化版本

Phase 1 固定：

```text
NORMALIZATION_VERSION = "markdown-normalizer-v1"
```

任何会改变规范化输出的行为修改都必须提升版本，并同步提升
`INTAKE_SCHEMA_VERSION`。

### 8.2 允许的规范化

按顺序执行：

1. 以 UTF-8 Python `str` 接收输入。
2. 如果第一个字符是 BOM `U+FEFF`，只删除开头这一个 BOM。
3. 把 `\r\n` 转为 `\n`。
4. 把剩余单独的 `\r` 转为 `\n`。
5. 保留其余所有字符和尾部换行状态。

### 8.3 禁止的规范化

- 不做 Unicode NFKC/NFKD，因为可能改写 API 标识符。
- 不统一大小写。
- 不删除行尾空格。
- 不折叠连续空白。
- 不自动补或删除文档末尾换行。
- 不格式化 fenced code。
- 不对 Markdown inline 内容做 HTML decode。
- 不执行 embedded HTML、脚本或链接。

### 8.4 整文档 hash

```python
content_hash = sha256_text(normalized_markdown)
```

必须复用 `src.knowledge.models.sha256_text`，保持现有 64 位十六进制格式。

规范化结果是 Source Parts 和文档 hash 的共同事实来源。禁止使用原始字符串算文档 hash、
却用规范化字符串分 Parts，否则会破坏幂等和证据定位。

## 9. Markdown 解析与 Source Part 算法

### 9.1 解析器选择

Phase 1 使用 `markdown-it-py` token stream，而不是用正则直接拆 Markdown。选择原因：

- 提供 CommonMark 解析。
- block token 暴露 `[line_begin, line_end]` source map。
- fenced code、列表、blockquote、HTML block 等可作为结构块识别。
- inline token 与 block token 分离，便于读取 heading 文本但保留原始 Source slice。

实现只消费 token，不渲染 HTML。解析器关闭 linkify，不加载需要网络或执行代码的插件。
表格支持应显式启用并有单测；如果所选 preset 不含 table rule，单独启用 table rule。
依赖通过 `uv add markdown-it-py` 纳入锁文件；实现 Agent 必须验证它与仓库当前
`requires-python` 兼容，不得为安装该依赖顺手改变项目 Python 版本下限。

参考：

- <https://markdown-it-py.readthedocs.io/en/latest/using.html>
- <https://markdown-it-py.readthedocs.io/en/latest/api/markdown_it.token.html>
- <https://markdown-it-py.readthedocs.io/en/latest/security.html>

### 9.2 行号到字符偏移

先扫描规范化文本，构造 `line_starts`：

```python
line_starts = [0]
for index, char in enumerate(markdown):
    if char == "\n":
        line_starts.append(index + 1)
```

token map `[start_line, end_line]` 的 `end_line` 是 exclusive：

```python
start = line_starts[start_line]
end = line_starts[end_line] if end_line < len(line_starts) else len(markdown)
source_slice = markdown[start:end]
```

不得使用渲染后的 HTML 或 token.content 重建 SourcePart；Part 必须是规范化 Markdown 的原文
切片。

### 9.3 Section 识别

1. 文档开头到第一个 heading 之前为 root section；如果只含空白，则并入第一个 heading
   section，不单独创建 whitespace-only Part；如果没有 heading，则仍由 root section 覆盖全文。
2. 每个 ATX/Setext heading 开始一个新 section。
3. section 结束于下一个任意层级 heading 的开始位置或 EOF。
4. 使用 heading level 维护栈；遇到 `hN` 时弹出所有 level `>= N` 的项，再压入当前 heading。
5. `heading_path` 保存栈中的 heading 显示文本，保留大小写。
6. heading 本身属于它开启的 section，确保文档所有字符都能被覆盖。

### 9.4 顶层原子块

为超长 section 寻找安全切分点时，只使用顶层 block token 的 source map。以下块默认不可
内部拆分：

- fenced code / indented code
- table
- blockquote 容器
- list 容器
- HTML block

普通 paragraph 可以在必要时拆分：优先选择不超过上限的最后一个换行；没有换行时在 Unicode
code point 边界拆分。不得按 UTF-8 byte 下标切字符串。

### 9.5 默认限制

集中放在 `constants.py`，允许未来由配置层覆盖，但 Phase 1 不暴露为每请求参数：

```text
MAX_DOCUMENT_BYTES        = 131072   # 128 KiB，规范化前 UTF-8 大小
MAX_NORMALIZED_CHARS      = 100000
TARGET_PART_CHARS         = 6000
MAX_PART_CHARS            = 10000
MAX_ATOMIC_BLOCK_CHARS    = 24000
MAX_PARTS                 = 128
MAX_METADATA_JSON_BYTES   = 16384    # 每个 metadata 字段
```

规则：

- 不可拆原子块超过 24,000 chars 时返回 422
  `KNOWLEDGE_INTAKE_ATOMIC_BLOCK_TOO_LARGE`，不把它悄悄切坏。
- 普通 Part 目标约 6,000 chars，硬上限 10,000 chars。
- 一个被允许保留的不可拆原子块可以形成超过普通 Part 上限、但不超过原子块上限的 Part，
  并在 preparation warnings 中记录。
- 最终超过 128 Parts 时拒绝整份请求，不发布部分结果。

这些限制控制同步 LLM 请求的 token 风险。后续如需处理更大文档，应走 Phase 3 异步批处理，
而不是无限提高同步入口上限。

### 9.6 Greedy 分块

对 section 内原子块按 source order 处理：

1. 当前 Part 为空时加入块。
2. 加入下一个块后若不超过 `MAX_PART_CHARS`，继续加入。
3. 若会超过上限，则关闭当前 Part，再从下一个块开始。
4. 普通 paragraph 自身超过上限时按 9.4 规则拆分。
5. 不可拆原子块超过普通上限但不超过原子块上限时单独成 Part。
6. 保留块之间的原始空白，使各 Part 是连续原文 slice。

### 9.7 完整覆盖不变量

分块必须满足：

```python
"".join(part.content for part in parts) == normalized_markdown
```

同时满足：

- 每个 Part 非空。
- Part slice 不重叠、无空洞、顺序严格递增。
- `order == tuple(range(len(parts)))`。
- `part_id` 唯一。
- `parent_part_id` 只能指向当前文档内更早的 Part。
- parent graph 无环。

完整覆盖测试是 Phase 1 的硬门禁。它能防止 Markdown 中的空白、标题、代码围栏或表格分隔符
在分块时丢失，从而保证 quote 和坐标可追溯。

### 9.8 稳定 Part ID

Part ID 不包含 Part content hash，避免正文小改动导致所有身份变化。

先生成 section key：

```text
document_id
\x1f partitioner_version
\x1f heading_path_with_levels
\x1f duplicate_heading_occurrence
```

再加入 section 内 segment index：

```python
part_id = "part_" + sha256_text(
    section_key + "\x1f" + str(segment_index)
)[:32]
```

细则：

- heading identity 使用 `level:title`，标题只做首尾 trim 和连续空白折叠，保留大小写。
- 相同 heading path 重复出现时增加从 0 开始的 occurrence。
- root section 使用固定 heading key `__root__`。
- Partitioner 行为变化必须提升 `PARTITIONER_VERSION`。
- 修改标题、插入同路径的更早重复标题或改变 section 的切分数量仍可能改变后续 Part ID；这是
  已知且可接受的边界，应通过稳定算法和版本化减少无意义漂移，而不是承诺绝对稳定。

### 9.9 Parent 拓扑

- section 的第一个 Part 指向最近的更低 heading level section 的第一个 Part。
- root section 的 parent 为 `None`。
- 顶层 heading section 的 parent 为 `None`。
- 同一 section 的 continuation Parts 指向该 section 的第一个 Part。
- 子 heading 始终指向父 heading section 的第一个 Part，而不是父 section 的最后一个
  continuation Part。

这样 parent 同时表达文档层级和长 section 的 continuation 归属。

## 10. `WikiUpdateInput` 构造

`WikiUpdateInputBuilder` 只做纯映射，不调用 LLM、不访问数据库：

```python
document = KnowledgeDocumentInput(
    document_id=raw.document_id,
    wiki_id=raw.wiki_id,
    rag_collection_id=raw.rag_collection_id,
    namespace=raw.namespace,
    version=raw.version,
    content_hash=sha256_text(normalized_markdown),
    source_path=raw.source_path,
    source_url=raw.source_url,
    source_origin=SourceOrigin(
        system="knowledge-intake",
        collection=raw.rag_collection_id or None,
        path=raw.source_path,
        url=raw.source_url,
        metadata={
            "normalization_version": NORMALIZATION_VERSION,
            "parser_version": PARSER_VERSION,
            "partitioner_version": PARTITIONER_VERSION,
        },
    ),
    source_metadata={
        **raw.source_metadata,
        "knowledge_intake": {
            "normalization_version": NORMALIZATION_VERSION,
            "parser_version": PARSER_VERSION,
            "partitioner_version": PARTITIONER_VERSION,
            "intake_schema_version": INTAKE_SCHEMA_VERSION,
        },
    },
    metadata=raw.metadata,
    parts=parts,
)

wiki = WikiUpdateInput(
    wiki_id=raw.wiki_id,
    rag_collections=(
        RagCollectionInput(
            rag_collection_id=raw.rag_collection_id,
            documents=(document,),
        ),
    ),
)
```

合并 metadata 前必须阻止调用方覆盖保留 key `knowledge_intake`。若调用方已提供同名 key，
返回 422，而不是静默覆盖或被覆盖。

## 11. 编译器身份与缓存失效

### 11.1 服务端基础 Options

组合根在创建 Extractor 时使用同一组局部变量创建基础 Options：

```python
extractor_version = "openai-compatible-v1"
prompt_version = "knowledge-compiler-v1"
intake_schema_version = "knowledge-1.md-intake-1"

extractor = OpenAIKnowledgeExtractor(
    client,
    model=model,
    extractor_version=extractor_version,
    prompt_version=prompt_version,
)

intake_base_options = UpdateOptions(
    extractor_version=extractor_version,
    prompt_version=prompt_version,
    model=model,
    schema_version=intake_schema_version,
)
```

禁止在不同文件中复制字符串后依赖“它们应该一致”。它们必须在同一组合点构造，或由一个
不可变的 compiler descriptor 产生。

### 11.2 合并运行选项

```python
resolved_options = intake_base_options.model_copy(
    update={
        "actor": request.publish.actor,
        "force_reprocess": request.publish.force_reprocess,
        "update_indexes": request.publish.update_indexes,
    }
)
```

调用方永远不能覆盖 compiler identity。

### 11.3 Intake 算法升级

现有 no-op 判断不读取 `source_metadata`，所以只更新 metadata 中的 parser version 不会触发
重新加工。任何以下变化都必须提升 `INTAKE_SCHEMA_VERSION`：

- 规范化规则变化。
- Parser preset/rule 变化。
- section 识别变化。
- 分块阈值发生会改变 Part 边界的变化。
- Part ID 或 parent 拓扑算法变化。

`INTAKE_SCHEMA_VERSION` 进入 `UpdateOptions.schema_version`，因此 compiler fingerprint 会变化，
相同原文也会重新编译并生成新 Source Revision。

## 12. `KnowledgeIntakeService` 编排

建议接口：

```python
class KnowledgeIntakeService:
    def __init__(
        self,
        publisher: KnowledgeWikiPublisher,
        parser: MarkdownDocumentParser,
        partitioner: SourcePartPartitioner,
        *,
        base_update_options: UpdateOptions,
        limits: IntakeLimits | None = None,
        observer: IntakeObserver | None = None,
    ) -> None: ...

    async def intake(
        self,
        document: RawMarkdownDocument,
        publish: KnowledgeIntakePublishOptions,
        *,
        request_id: str,
    ) -> KnowledgeIntakeResult: ...
```

执行顺序：

1. 检查 UTF-8 byte size、metadata size 和非空内容。
2. 规范化 Markdown，计算行偏移和整文档 hash。
3. 解析 token stream。
4. 生成 section、Source Parts 和 parent topology。
5. 执行完整覆盖、Part 数量、唯一性和拓扑自检。
6. 构造 `KnowledgeDocumentInput`、`RagCollectionInput`、`WikiUpdateInput`。
7. 合并服务端 compiler options 与调用方运行选项。
8. 调用 `publisher.update_wiki(wiki, resolved_options)`。
9. 返回 preparation report、UpdateResult 和 Intake Trace。

准备阶段任一步失败时：

- 不调用 `update_wiki()`。
- 不写 Source Revision、staging、catalog 或索引。
- 返回稳定错误码和不包含原文的错误摘要。

## 13. Trace 与可观测性

### 13.1 与 Query Trace 分离

查询 Trace 保持：

```text
trigger → recall → rerank → inject → generate
```

Phase 1 Intake Trace 只记录 Intake 层实际可观测的阶段：

```text
normalize → parse → partition → build → update
```

不能把 `compile/validate/publish/index` 分别伪装成 Intake 阶段，因为当前 Intake 只调用一个
黑盒式 `update_wiki()`，无法准确测量这些内部阶段。若未来要拆分写入内部 Trace，应另立
Knowledge Update Trace 设计，并在修改 Service 前做影响分析。

### 13.2 Trace 字段

每个 stage 至少包含：

- `name`
- `status`: `completed | failed | skipped`
- `duration_ms`
- 安全的 `details`

允许的 details：

- byte/char 数量。
- token/section/part 数量。
- hash 前 12 位或完整内容 hash。
- parser/partitioner/schema 版本。
- UpdateResult 的 status 和 operation_id。

禁止记录：

- 完整 Markdown。
- `quote_hint` 全文集合。
- 完整 Prompt 或 LLM response。
- API key、Authorization header、provider 原始异常。
- 可能含私有 API 的 metadata 全量内容。

### 13.3 Phase 1 落地方式

- Trace 随 Intake Response 返回。
- 使用 `logging` 输出一条 request summary 和每阶段结构化事件，全部带 `request_id`。
- 若运行环境已有 OpenTelemetry tracer，则通过可选 `IntakeObserver` 适配；Phase 1 不为此单独
  引入持久化表或强制新增 OTel 依赖。
- Trace 落库后端仍是项目 Open Decision，不在本功能中擅自拍板 Postgres/ClickHouse。

## 14. Evidence 坐标策略

Phase 1 不在 Intake 层修复 Evidence，原因是 Evidence 在 `update_wiki()` 内部调用 Extractor
后才产生，Intake 层拿不到 `ExtractionResult`。

现有安全策略继续生效：

- `content_hash` 必须匹配声明的 SourcePart。
- `quote_hint` 必须是 SourcePart 的连续原文。
- 若提供 char range，必须在 Part 边界内且覆盖 quote。
- 校验失败则该文档不发布。

Phase 2 如需坐标修复，应实现 `KnowledgeExtractor` decorator 或明确的 extraction-normalization
hook，规则为：

1. `quote_hint` 在声明 Part 内唯一出现时，可由服务端重算精确范围。
2. 多次出现时，只接受已经精确覆盖某次出现的原范围。
3. 多次出现且原范围无效时标记 needs_review，不任意选择第一次。
4. 不允许跨 Part 搜索后悄悄改写 `part_id`。

## 15. 安全设计

- 路由复用 `require_service_auth`，默认安全关闭。
- 原始 Markdown 永远视为不可信数据。
- Intake Parser 只生成 token，不渲染、不执行 HTML/JS、不访问链接。
- Phase 1 不读取文件路径，不抓取 URL，不访问外部网络。
- `source_path`、`source_url` 仅作 provenance metadata。
- 限制文档、metadata、Part 数量和不可拆块大小，避免内存/Prompt 放大攻击。
- 不在日志中记录原文、Prompt、provider response 或凭据。
- namespace/version 原样保存，不做大小写归一化。
- 保留现有 Extractor system prompt 的“原始文档是不可信数据”约束。
- 所有发布仍经过 Evidence、Scope 和 relation 的确定性校验。
- URL 抓取到 Phase 3 才实现，并且必须接入 `docs/ingest-sources.md` 白名单、robots、限流、
  重试和 SSRF 防护。

## 16. 运行时装配

### 16.1 App state

在 `lifespan()` 成功构造 `knowledge_update_service` 后构造：

```python
app.state.knowledge_intake_service = KnowledgeIntakeService(...)
```

依赖降级：

- Mongo 不可用：Intake Service 不装配。
- LLM provider key 缺失：Knowledge Update Service 不装配，Intake 返回 503 disabled。
- Redis 不可用：不影响 Intake。
- Zvec index writer 异常：沿用 UpdateResult partial 语义，不由 Intake 回滚 catalog。

### 16.2 Gateway dependency

新增：

```python
def get_knowledge_intake_service(request: Request) -> KnowledgeIntakeService | None:
    return cast(
        KnowledgeIntakeService | None,
        getattr(request.app.state, "knowledge_intake_service", None),
    )
```

### 16.3 Router 注册

- `src/gateway/__init__.py` 导出新 router。
- `src/main.py` import 并执行 `app.include_router(knowledge_intake_router)`。
- prefix 继续使用 `/api/v1/knowledge`，子路径 `/intake`。
- tag 使用 `Knowledge Wiki 写入` 或 `Knowledge Intake`，保持 OpenAPI 可发现。

## 17. 测试方案

### 17.1 Phase 0 契约测试

在现有 Knowledge 测试中新增：

1. `EvidenceRef(rag_collection_id="")` 合法。
2. Document collection 为空、Evidence collection 为空时通过来源校验。
3. Document collection 非空、Evidence collection 为空时仍失败。
4. OpenAI Extractor 返回独立 Wiki Evidence 时可以完成 `ExtractionResult` 校验。
5. 现有所有非空 collection 测试保持通过。

### 17.2 Normalization 单测

文件：`tests/unit/test_knowledge_intake_normalization.py`

- BOM 只在文首删除。
- CRLF/CR 转 LF。
- 代码内容、行尾空格、大小写和 Unicode 标识符不被改写。
- 末尾换行状态保持。
- 相同规范化结果产生相同 hash。
- byte/char 限制分别生效。

### 17.3 Parser/Partitioner 单测

文件：`tests/unit/test_knowledge_intake_partitioner.py`

- 无 heading 文档生成 root Part。
- H1/H2/H3 产生正确 heading_path 和 parent。
- Setext heading 可识别。
- 重复标题产生唯一且确定的 ID。
- 正文小改动但标题结构不变时 Part ID 尽量稳定。
- fenced code 不被拆开。
- table/list/blockquote 不被内部拆开。
- 超长普通 paragraph 可确定性拆分。
- 超长不可拆块返回稳定错误。
- 超长 section greedy 分块满足上限。
- continuation Part parent 指向 section 首 Part。
- Parts 严格递增、无环、ID 唯一。
- `join(part.content) == normalized_markdown`。
- 相同输入连续运行产生完全相同的 model dump。
- 超过最大 Parts 时整份失败。

### 17.4 Builder 单测

文件：`tests/unit/test_knowledge_intake_builder.py`

- Scope 原样复制，包括官方大小写。
- 文档 hash 来自规范化全文。
- `SourceOrigin` 和 intake 版本 metadata 正确。
- 空 `rag_collection_id` 可构造独立 Wiki。
- 保留 metadata key 冲突时拒绝。
- `WikiUpdateInput.documents` 只有一个目标文档。

### 17.5 Service 单测

文件：`tests/unit/test_knowledge_intake_service.py`

使用 Fake Publisher：

- 成功时只调用一次 `update_wiki()`。
- 准备失败时 Publisher 调用次数为零。
- compiler identity 来自服务端 base options。
- 请求只能覆盖 actor/force/update_indexes。
- 返回 preparation report 和五个 Intake stages。
- Publisher 返回 failed/partial 时 Intake 仍返回其完整 UpdateResult。
- 重复内容配合真实 InMemory Knowledge Repository + Fake Extractor：首次一次 Extractor 调用，
  第二次 unchanged 且不再调用 Extractor。

### 17.6 Gateway 单测

文件：`tests/unit/test_knowledge_intake_gateway.py`

- 未配置 Service 返回 503 disabled。
- 未授权返回 401；未配置鉴权返回 503。
- 缺少 wiki_id/namespace/version/document_id 返回 422。
- whitespace-only Markdown 返回 422。
- 过大请求返回 413 和稳定错误码。
- Fake Service 成功返回 200 与 request_id。
- Service 意外异常返回脱敏 503，不泄露异常字符串。
- OpenAPI 中存在 `/api/v1/knowledge/intake`。

### 17.7 端到端测试

使用：

- FastAPI TestClient。
- 真实 Intake normalizer/parser/partitioner/builder/service。
- `InMemoryKnowledgeRepository`。
- Fake KnowledgeExtractor。
- InMemory index writer。

Fixture 包含：标题层级、普通段落、表格、代码块和重复标题。断言：

- 生成期望数量和拓扑的 Source Parts。
- Fake Extractor 看到的 Part hash 与 SourcePart 派生 hash 一致。
- Artifact 成功发布且 Evidence 可定位。
- 第二次相同请求返回 unchanged。
- 第二次不调用 Fake Extractor。

### 17.8 Benchmark

Phase 1 不新增检索策略，因此不要求修改检索 Benchmark 排名基线；但应新增一个可复现 golden
fixture：

```text
tests/fixtures/knowledge_intake/
├── api_reference.md
└── api_reference.expected.json
```

expected JSON 至少记录 normalization/parser/partitioner 版本、document hash、Part IDs、orders、
parents、heading paths 和每个 Part 的内容 hash。算法有意升级时必须显式更新版本和 golden。

## 18. 验收标准

以下条件全部满足才算 Phase 1 完成：

- [ ] `/api/v1/knowledge/intake` 接受单份 Markdown 并返回机器可消费结果。
- [ ] `wiki_id/document_id/namespace/version` 缺一不可，且原样进入领域模型。
- [ ] Intake 准备阶段没有 LLM 调用。
- [ ] 首次有效请求最多一次 Extractor 调用；重复请求为零次。
- [ ] Parts 对规范化 Markdown 完整覆盖、无重叠、无空洞。
- [ ] 代码块、表格、列表和 blockquote 不被内部切坏。
- [ ] Part IDs 对相同输入完全确定。
- [ ] 文档 hash 与规范化全文一致；Part hash 仍由 SourcePart 派生。
- [ ] 空 collection 的独立 Wiki 能通过真实 ExtractionResult schema 和来源校验。
- [ ] compiler metadata 不再因 intake 默认 options 产生 mismatch。
- [ ] 任何准备失败都不会调用 Repository/Publisher。
- [ ] 现有 `/api/v1/knowledge/update` 行为和 schema 不变。
- [ ] Query 五阶段 Trace 和 `TraceStage` 不变。
- [ ] staging/catalog 原子发布和 index partial 语义不变。
- [ ] 新增单测、E2E 和 golden fixture 全部通过。
- [ ] `ruff check`、`ruff format --check`、`mypy --strict` 通过项目要求的范围。
- [ ] GitNexus `detect_changes(scope="compare", base_ref="main")` 只显示预期模块和流程。

## 19. 实施顺序与建议提交

### Step 0：工作区保护

1. 执行 `git status --short`。
2. 保留用户已有未提交修改，不覆盖无关文件。
3. 检查 GitNexus 索引 freshness；过期则刷新。
4. 每次编辑现有 function/class/method 前执行 upstream impact。

### Step 1：Phase 0 契约修复

1. 对 `EvidenceRef` 运行 impact 并报告 CRITICAL 风险。
2. 放宽空 collection 契约。
3. 增加独立 Wiki Evidence 测试。
4. 跑完整 Knowledge update/query/graph 相关单测。

建议提交：

```text
fix(knowledge): align standalone wiki evidence provenance
```

### Step 2：确定性 Intake 核心

1. 添加 parser 依赖。
2. 实现 constants/models/contracts。
3. 实现 normalization 和 line offset。
4. 实现 parser token 适配。
5. 实现 partitioner、stable ID、topology 和自检。
6. 实现 builder。
7. 完成核心单测与 golden。

建议提交：

```text
feat(intake): prepare deterministic wiki inputs from markdown
```

### Step 3：Service 与 compiler options

1. 实现 `KnowledgeIntakeService`。
2. 在组合根用同一组值构造 Extractor 与 intake base options。
3. 完成 idempotency 和单 LLM 调用测试。

建议提交：

```text
feat(intake): delegate prepared documents to knowledge publisher
```

### Step 4：Gateway

1. 添加 schemas/router。
2. 复用 auth。
3. 注册 router 和 app state。
4. 添加错误映射和 Gateway 测试。

建议提交：

```text
feat(gateway): expose authenticated markdown intake endpoint
```

### Step 5：文档与最终验证

1. 更新 `docs/knowledge-update-plane.md` 和模块 README。
2. 运行 format/lint/typecheck/unit tests。
3. 运行 GitNexus `detect_changes(compare main)`。
4. 检查 API 示例和 OpenAPI schema。

建议提交：

```text
docs(intake): document raw markdown knowledge ingestion
```

## 20. 必跑命令

实现 Agent 应根据仓库当前 Python/uv 配置执行：

```bash
uv run pytest tests/unit/test_knowledge_update.py
uv run pytest tests/unit/test_knowledge_update_gateway.py
uv run pytest tests/unit/test_knowledge_intake_normalization.py
uv run pytest tests/unit/test_knowledge_intake_partitioner.py
uv run pytest tests/unit/test_knowledge_intake_builder.py
uv run pytest tests/unit/test_knowledge_intake_service.py
uv run pytest tests/unit/test_knowledge_intake_gateway.py
uv run pytest tests/unit
uv run ruff format --check .
uv run ruff check .
uv run mypy .
```

如果仓库当前全量检查已有与本功能无关的基线失败，必须：

1. 记录完整命令和失败摘要。
2. 证明新增/修改文件的定向检查通过。
3. 不顺手修改无关基线问题。

提交前必须执行 GitNexus：

```text
detect_changes(scope="compare", base_ref="main")
```

## 21. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| 修改 `EvidenceRef` 影响面大 | 可能破坏写入、查询、图和测试模型 | 独立 Phase 0；impact 警告；兼容性放宽；完整回归 |
| 分块算法升级但缓存未失效 | 新算法不会应用到相同原文 | 所有影响边界的变化提升 `INTAKE_SCHEMA_VERSION` |
| Part ID 漂移 | 旧 Evidence 对齐变差 | ID 不含正文 hash；版本化算法；golden fixture |
| Markdown 超长导致 Prompt 爆炸 | LLM 超限、延迟和成本上升 | 同步入口硬限制；Part/atomic block 上限；Phase 3 队列 |
| 代码/表格被切坏 | Evidence 和语义失真 | AST token；不可拆原子块；完整覆盖测试 |
| Scope 猜错 | 污染错误 Wiki/版本 | 四个身份字段必传；不推断、不归一化 |
| compiler options 被调用方伪造 | 缓存和元数据门禁失真 | Intake API 只开放运行选项，身份由组合根注入 |
| Trace 泄露文档 | 私有 API 内容外泄 | 只记 hash/计数/版本；禁止原文和 Prompt |
| 重复两次 LLM | 成本翻倍且结果不一致 | Intake 纯确定性；Claim 只由现有 Extractor 生成 |
| Evidence offset 漂移 | 校验失败、文档不发布 | Phase 1 fail closed；Phase 2 唯一引用确定性修复 |

## 22. Phase 2 / Phase 3 路线图

### Phase 2

- `/api/v1/knowledge/intake/preview`：只返回准备后的 `WikiUpdateInput` 和报告，不发布。
- YAML 与 HTML adapter，统一进入同一个 canonical document/partition contract。
- Scope/version 建议器：只返回 suggestion + confidence + evidence，不自动发布。
- Evidence 正规化 decorator。
- JSON Schema 导出与 CLI 校验工具。

### Phase 3

- 多文档批量 Intake，每文档独立结果。
- 异步队列、作业状态、重试上限和死信处理。
- URL 抓取、白名单、robots、限流、重试、SSRF 防护。
- 大文档分阶段编译与成本预算。
- Raw document → WikiUpdateInput → Artifact 的长期 Benchmark 和归因报告。

## 23. Agent 执行约束

交给其他 Agent 实施时，必须遵守以下约束：

1. 先读仓库根 `AGENTS.md`，其内容高于本文的工程建议。
2. 不得假设工作区干净，不得覆盖用户未提交修改。
3. 探索未知流程先使用 GitNexus query/context。
4. 编辑任何已有 function/class/method 前先做 upstream impact。
5. HIGH/CRITICAL 必须先向用户报告，再继续编辑。
6. 新文件可以按本文创建，但修改现有公共模型必须最小化。
7. 不用 find-and-replace 重命名符号。
8. 不改变 `/knowledge/update`、Query Trace 或 catalog 发布语义来“顺便优化”。
9. 不用 LLM 做可由确定性程序完成的分块、hash、ID 或 Scope 决策。
10. 不提交密钥、原始私有文档、完整 Prompt 或 provider response。
11. 提交前执行 `detect_changes(compare main)` 并审阅影响流程。
12. 若实现与本文产生不可兼容分歧，应停止并请求拍板，不得自行扩大 Scope。

## 24. 最终拍板

Phase 0 + Phase 1 可以按本文直接实施。最终产品边界为：

```text
Knowledge Intake = 确定性原始文档准备层
Knowledge Extractor = LLM 知识编译层
Knowledge Update Service = 唯一验证与发布层
```

三层职责不得合并。这个边界既避免重复 LLM 调用，也保留现有 Knowledge 核心域、版本隔离、
证据门禁和原子发布语义。
