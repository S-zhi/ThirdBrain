# CANN Judge 算子开发 Benchmark

这个适配器把 [CANN Judge](https://cannjudge.cn/home) 的公开题库转换为两类本地资产：

1. `operator_scenarios.jsonl`：代码 Agent 可直接执行的算子工程任务，带 CANN 版本、命名空间、芯片、工程模板、公开通过率和远端 Judge 契约。
2. `docs/*.md`：兼容 `benchmark/generate/generate_isok_data` 的 Markdown 语料，可继续生成现有问答型 benchmark。

同步器只访问公开只读 API，不读取 cookie，不保存账号密码，也不会自动提交代码。在线提交页需要登录，后续应通过独立、显式授权的 Judge adapter 接入。

## 快速开始

默认同步公开的 S1、S2 算子题：

```bash
uv run python -m benchmark.cannjudge.sync
```

指定赛事和输出位置：

```bash
uv run python -m benchmark.cannjudge.sync \
  --contest s1 \
  --contest s2 \
  --output benchmark/cannjudge/generated/operator_scenarios.jsonl \
  --docs-dir benchmark/cannjudge/generated/docs
```

把同步后的题面继续送入现有问答数据生成器：

```bash
uv run python benchmark/generate/generate_isok_data/run.py \
  --docs_dir benchmark/cannjudge/generated/docs \
  --count 25 \
  --output benchmark/cannjudge/generated/rag_questions.jsonl
```

## 场景结构

每条场景遵循 `operator-development.v1`：

- `namespace` 固定为 `Huawei.CANN.AscendC.<cann_version>`，保证 version-first 和命名空间隔离；
- `prompt` 保留完整题面，作为代码 Agent 的开发任务；
- `judge` 描述远端隐藏测试与提交地址，但明确标注 `requires_auth`；
- `observed_stats` 记录公开通过人数、尝试人数和通过率，只作为难度代理，不作为模型得分；
- `source` 保留 group / contest / problem 标识，便于增量同步和溯源。

正式评分建议以 CANN Judge 隐藏测试结果为 correctness 主分，同时单独记录检索命中率、API 幻觉率、编译成功率、首次通过率和迭代次数，避免把网站公开通过率误当作被测模型成绩。
