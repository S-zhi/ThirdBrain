# JSONL Model Output 生成工具

读取 `generate_isok_data/output.jsonl` 中的每条 case，根据 `--mode` 选择公开互联网搜索
对照组或内部 RAG ID 精确检索组。两组使用相同的无历史最终回答器，回答写入新增的
`model_output` 字段，输出始终写到新 JSONL，原文件不会被覆盖。

## 两种流程

推荐用于正式对照的 `web` 流程：

```text
question → MiniMax 规划 1～N 条网络查询
         → Tavily Search 查询公开互联网资料
         → question + 本轮网络资料 → 全新 MiniMax 请求 → model_output
```

实验组 `rag_id` 流程：

```text
case.source_docs → 在内部 RAG 语料目录按原始文件 ID 精确读取文档
                 → question + 本轮内部文档 → 全新 MiniMax 请求 → model_output
```

两个流程共用 `prompts/retrieval_answer.md` 和完全相同的输出限制。网络组的查询规划和最终回答
是两个互相独立的 MiniMax 请求，不传递聊天历史；RAG 组直接使用 benchmark 已有文档 ID，
不让模型猜测内部 ID，也不会把不同路径下的同名 API 合并。保留 `direct` 作为兼容模式，但正式
检索对照建议使用 `web` 与 `rag_id`。

## 输出限制

- `tag == "编写代码"`（兼容 `Coding` / `Code`）：首次 `model_output` 最多 500 字。
- 其他 case：首次 `model_output` 最多 200 字。
- 提示词会要求模型遵守限制，写入前还会按 Unicode 字符数执行硬截断。
- `question` 会完整发送，不截断问题语义。
- 如果普通 case 的已有结果正好达到 `200/200`，下次运行会自动将该条上限提升到 500 字并重跑。
- 如果任何 case 的已有结果达到 `500/500`，下次运行会自动将该条上限提升到 1500 字并重跑。
- 1500 字是最终上限，不再继续扩张。
- `model_output_max_chars` 记录本次实际使用的上限，避免完成升级的结果被重复识别。

## 运行

```bash
cd benchmark/generate/generate_isok_data_by_llm

# 兼容的无检索模式
./run.sh

# 公开互联网搜索对照组；密钥只从环境变量读取
export TAVILY_API_KEY='你的 Tavily API Key'
./run.sh --mode web

# 内部 RAG ID 精确检索组
./run.sh --mode rag_id

# 自定义并发和输出文件
WORKERS=30 ./run.sh --output ./my_model_output.jsonl

# 自定义内部 RAG 语料目录
WORKERS=30 ./run.sh --mode rag_id \
  --rag-documents-dir ../generate_isok_data/md \
  --rag-context-max-chars 40000
```

默认输出相互隔离：

- `direct`：`output_with_model_output.jsonl`
- `web`：`output_with_web_model_output.jsonl`
- `rag_id`：`output_with_rag_id_model_output.jsonl`

已有输出会校验 `generation_mode`。即使手动传入相同 `--output`，也不会把 web 与 rag_id 的
同名 question 错误识别为同一组结果。

`rag_id` 默认从 `../generate_isok_data/md` 读取 `source_docs` 指定的 Markdown。RAG 参数：
`--rag-documents-dir`、`--rag-context-max-chars`。

网络参数：`--web-api-url`、`--web-max-queries`、`--web-max-results`、
`--web-context-max-chars`、`--web-timeout`。网络组会产生 Tavily Search 用量，建议先使用小输入
和较低并发验证。搜索请求会获取清洗后的原网页 Markdown；网页正文不可用时才使用搜索摘要，
最终传给 MiniMax 的总内容仍受 `--web-context-max-chars` 限制。

## 可编辑提示词

- `prompts/direct_answer.md`：direct 最终回答。
- `prompts/web_query_planner.md`：公开网络查询规划，可调整 query 数量和组合策略。
- `prompts/retrieval_answer.md`：web 与 rag_id 共用的最终回答提示词。

`web` 输出额外记录网络 queries、结果 URL 和检索摘要；`rag_id` 输出额外记录精确命中的原始
source ID 和文档数，便于公平复核两组拿到了什么资料。

## 进度与断点续跑

每条 case 完成后立即追加写入并显示原输入行号：

```text
✓ [37/298] 输入行 42 [186/200字] 如何使用...
```

任务中断后，直接执行相同命令即可。工具会使用 `question` 作为 case 名称，读取已有输出并跳过
已经存在的 `question`，只请求尚未完成的 case。同名重复 case 会按出现次数匹配，不会全部误跳过。
扫描发现结果达到当前上限时，会分别显示 `200→500重跑` 和 `500→1500重跑` 数量，只重跑
这些记录并提升对应上限；成功后旧结果会被新结果替换。若中途再次中断，旧记录仍会保留并在
下次继续重跑。若要重新生成全部结果，显式传入 `--clear`；它只清理新输出，不会修改输入
JSONL。

单条模型请求失败不会中断其他任务，失败记录不会写入，因此下次运行会自动重试。只要存在失败，
本次进程会返回非零退出码。
