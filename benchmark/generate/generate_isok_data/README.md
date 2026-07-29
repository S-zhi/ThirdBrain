   # Benchmark 评测数据生成工具

从 API 文档自动生成评测问题、标准答案和评判规则，用于评估 RAG 对 LLM 代码生成能力的帮助程度。

## 工作流程

```
批量文档 → 全局扫描 → API选取 → 问题生成 → 标准答案生成 → JSONL输出
```

1. **全局扫描**（节点2）：每个文档独立请求一次 MiniMax，提取 API 列表、摘要、模块；相似组按单文档主题在本地归组，不再发起集合请求
2. **API 选取**（节点3）：按概率策略选取文档（90% 随机 / 10% 对比）
3. **问题生成**（节点4）：基于选中 API 文档并发生成问题，自动分配标签
4. **答案生成**（节点5）：并发生成标准答案 + 评估说明
5. **输出**（节点6）：JSONL 格式，每行一条评测数据

## 快速开始

### 1. 安装依赖

```bash
uv sync
```

### 2. 配置 MiniMax API

推荐通过环境变量或当前目录下的 `.env` 配置，不要把密钥提交到仓库：

```bash
export MINIMAX_BASE_URL="https://api.minimaxi.com/v1"
export MINIMAX_API_KEY="your-minimax-api-key"
export MINIMAX_MODEL="MiniMax-M2.7-highspeed"
```

默认模型是适合批量生产的 `MiniMax-M2.7-highspeed`，也可将
`MINIMAX_MODEL` 设为 `MiniMax-M3`。为兼容旧配置，代码仍接受
`LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL_NAME`。

### 3. 运行

```bash
# 使用当前 md 文档，生成 3 条评测数据
python run.py --docs_dir ./md --count 3 --output output.jsonl

# 使用 8 个 worker 并发执行“问题生成 → 答案生成 → 落盘”
python run.py --docs_dir ./md --count 300 --workers 8 --output output.jsonl

# Mock 模式（跳过 LLM，测试流程）
python run.py --docs_dir ./md --count 2 --mock

# 覆盖输出文件
python run.py --docs_dir ./my_docs --count 10 --output benchmark.jsonl --clear
```

### CLI 参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--docs_dir` | API 文档目录路径 | `./md` |
| `--count` | 每轮生成的问题数量 | `1` |
| `--output` | 输出 JSONL 文件路径 | `output.jsonl` |
| `--mock` | Mock 模式（跳过 LLM） | `false` |
| `--clear` | 覆盖输出文件而非追加 | `false` |
| `--workers` | 单文档扫描和数据生产的并发数；每个 worker 复用独立 MiniMax 客户端 | `1` |

建议先用 `--workers 3` 到 `--workers 10`。若并发过高触发 MiniMax
限流，可调低该值；临时网络错误和限流默认由 SDK 最多重试 4 次，可通过
`MINIMAX_MAX_RETRIES` 调整。

## 输出格式

每行一条 JSON 记录：

```json
{
  "question": "如何使用 pandas.DataFrame.sort_values 按多列排序？",
  "tag": "简单用法",
  "answer": "```python\nimport pandas as pd\ndf = pd.DataFrame(...)\nresult = df.sort_values(by=['col1', 'col2'], ascending=[True, False])\n```\n\n说明：...",
  "evaluation_note": "关键判定点：必须使用 sort_values 函数。常见错误：ascending 参数与 by 长度不匹配。宽容点：变量命名不影响评分。",
  "source_docs": ["sample_api.md"],
  "selection_mode": "单API"
}
```

### 字段说明

| 字段 | 说明 |
|------|------|
| `question` | 评测问题文本 |
| `tag` | 标签：`简单用法` / `编写代码` / `平台适配` / `其他` |
| `answer` | 标准答案（代码 + 说明） |
| `evaluation_note` | 评估说明（≤ 200 字） |
| `source_docs` | 来源文档文件名列表 |
| `selection_mode` | 选取模式：`单API` / `多API对比` / `混合` |

## 项目结构

```
benchmark_workflow/
├── config.py          # 配置（LLM API、路径、策略参数）
├── scanner.py         # 节点2：全局浅扫
├── selector.py        # 节点3：API 选取（含概率策略）
├── question_gen.py    # 节点4：问题生成（含标签分配）
├── answer_gen.py      # 节点5：标准答案生成
├── pipeline.py        # 主流程串联
├── prompts/           # LLM 提示词模板
│   ├── scan.md        # 扫描提示词
│   ├── similarity.md  # 相似性检测提示词
│   ├── question.md    # 问题生成提示词
│   └── answer.md      # 答案生成提示词
├── run.py             # CLI 入口
├── README.md          # 本文件
└── md/                # 待生成 benchmark 的 API 文档
```

## 选取策略详解

### 随机模式（90% 概率）
- 从文档池中随机选取 1 个文档
- 有 10% 概率扩展为多选（追加 1-2 个相关文档）
- 扩展依据：相似组 > 同模块 > 随机补充

### 对比模式（10% 概率）
- 从相似 API 组中选取 2-3 个文档
- 生成对比性问题（"A 和 B 有什么区别？"）
- 若相似组源文件不足 2 个，退回随机模式

## 标签体系

| 标签 | 适用场景 |
|------|---------|
| 简单用法 | 单个 API 的基础调用，参数用法，返回值结构 |
| 编写代码 | 需要完整代码块，多 API 组合，完整功能实现 |
| 平台适配 | 特定 OS/框架/硬件的差异处理 |
| 其他 | API 对比、性能、错误处理、最佳实践 |
