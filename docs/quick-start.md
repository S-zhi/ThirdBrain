# 快速开始

本文帮助新用户在本地完成以下最短链路：

```text
安装依赖 → 配置环境 → 启动 MongoDB → 启动查询服务 → 查询 API 文档
```

项目使用 Zvec 保存 API 文档向量，使用 MongoDB 记录查询快照，使用阿里云
DashScope Qwen 生成查询向量。

## 1. 环境要求

- Python 3.14+
- [uv](https://docs.astral.sh/uv/)
- MongoDB 8.x，或者可访问的 MongoDB Atlas
- 阿里云百炼 DashScope API Key

以下命令均在项目根目录执行：

```bash
cd /Users/wenzhengfeng/code/agent/ragWithColdApiDocument
```

## 2. 安装依赖

```bash
uv sync
```

项目默认配置位于 `config.yaml`：

```yaml
embedder:
  type: bailian
  bailian:
    model: qwen3.7-text-embedding
    dimension: 2048

zvec:
  collection_path: ./data/zvec_collections
  default_collection: ascendc_api
```

不要把 API Key 写入 `config.yaml`。

## 3. 配置环境变量

复制示例配置：

```bash
cp .env.example .env
```

编辑 `.env`，至少确认以下配置：

```ini
DASHSCOPE_API_KEY=你的百炼APIKey
RAG_MONGO_URI=mongodb://127.0.0.1:27017
RAG_MONGO_DATABASE=rag_cold_api
RAG_MONGO_QUERY_RECORD_COLLECTION=agent_query_records
RAG_MONGO_INIT_MODE=auto
```

`.env` 已被 Git 忽略，不要提交密钥。执行摄取脚本或启动查询服务前，将 `.env`
载入当前终端：

```bash
set -a
source .env
set +a
```

这一步不能省略：MongoDB 配置能够自动读取 `.env`，但当前 DashScope embedding
实现要求 `DASHSCOPE_API_KEY` 已存在于进程环境中。

## 4. 启动 MongoDB

本地开发可以使用 Docker：

```bash
docker run -d \
  --name rag-mongodb \
  -p 27017:27017 \
  -v rag-mongodb-data:/data/db \
  mongo:8.0
```

如果容器已经创建，只需重新启动：

```bash
docker start rag-mongodb
```

更完整的 MongoDB 配置参见 [mongodb.md](mongodb.md)。

## 5. 确认 Zvec 集合

当前生产集合路径为：

```text
/Users/wenzhengfeng/code/agent/ragWithColdApiDocument/data/zvec_collections/ascendc_api
```

该集合当前保存 AscendC 910beta3 API 文档，包含 dense 2048 维语义向量和动态
sparse 关键词向量。

查询服务运行期间，不要同时在 Zvec Studio 中打开同一个集合。Studio 会持有
`LOCK`，导致服务进程无法打开集合。不要手动删除 `LOCK` 文件，应先在 Studio
关闭集合或停止 Studio。

## 6. 启动查询服务

确保当前终端已经执行过第 3 步的 `source .env`，然后运行：

```bash
uv run uvicorn src.main:app --reload
```

启动成功后可访问：

- API：<http://127.0.0.1:8000>
- Swagger UI：<http://127.0.0.1:8000/docs>
- OpenAPI JSON：<http://127.0.0.1:8000/openapi.json>

服务启动时会连接 MongoDB，并根据 `RAG_MONGO_INIT_MODE=auto` 创建缺失的
Collection 和索引。MongoDB 不可用时，服务会启动失败。

## 7. 查询一个 API

### 按名称精确查询

下面的请求从 Zvec 查询 `ListTensorDesc`：

```bash
curl -sS -X POST 'http://127.0.0.1:8000/api/v1/agent/query/once' \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "ListTensorDesc",
    "query_type": "name",
    "top_k": 5,
    "filters": {
      "namespace": "com.huawei.cann.ascendc.op.910beta3",
      "version": "910beta3",
      "language": "cpp"
    }
  }'
```

成功响应中的 `documents` 包含 API ID、签名、参数、返回值、示例、产品支持和
完整 Markdown 原文。名称查询的 `score` 为 `null`。

### 按自然语言查询

```bash
curl -sS -X POST 'http://127.0.0.1:8000/api/v1/agent/query/once' \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "在kernel侧按索引获取数据地址和shape信息",
    "query_type": "semantic",
    "top_k": 5,
    "filters": {
      "namespace": "com.huawei.cann.ascendc.op.910beta3",
      "version": "910beta3",
      "language": "cpp"
    }
  }'
```

`namespace` 和 `version` 是必填过滤条件，用于防止不同产品或版本的相似 API
互相污染。语义查询需要调用 DashScope，并可能产生 API 用量。

## 8. 摄取新的 YAML

建议先 dry-run 检查转换结果：

```bash
.venv/bin/python src/script/ingest.py \
  ingest/output/Sub/SIMD_API/其他数据类型/ListTensorDesc.yaml \
  --dry-run
```

确认后正式写入默认集合：

```bash
.venv/bin/python src/script/ingest.py \
  ingest/output/Sub/SIMD_API/其他数据类型/ListTensorDesc.yaml \
  --collection ascendc_api
```

摄取会调用 DashScope 分别生成 dense 和 sparse 向量。运行结果记录保存在：

```text
data/ingest_records/
```

同一批次出现重复 `chunk_id` 时，标准摄取脚本会跳过冲突项并在运行记录中报告，
不会无提示地用最后一条覆盖前面的内容。

## 9. 使用 Zvec Studio 查看集合

如果当前虚拟环境还没有 Zvec Studio：

```bash
uv pip install zvec-studio
```

启动 Studio：

```bash
.venv/bin/zvec-studio --host 127.0.0.1 --port 7860
```

浏览器访问 <http://127.0.0.1:7860>，然后打开第 5 节中的生产集合路径。

停止 Studio 时，在启动终端按 `Ctrl+C`。准备运行摄取或查询服务前，先在 Studio
中关闭集合，避免 Zvec 文件锁冲突。

## 10. 常见问题

### 提示缺少 `DASHSCOPE_API_KEY`

确认 `.env` 中已经配置 Key，并在当前终端重新执行：

```bash
set -a
source .env
set +a
```

### 提示 `Can't lock collection`

另一个进程正在使用集合。关闭 Zvec Studio 中的集合、停止 Studio，或者停止其他
查询/摄取进程。不要直接删除 `LOCK`。

### Studio 报 sparse `dimension=0` 校验错误

Zvec 使用 `dimension=0` 表示动态 sparse vector，这是合法 Schema。当前项目虚拟
环境已包含兼容修复；重新安装或升级 `zvec-studio` 可能覆盖该修复。详见项目根
[README](../README.md#sparse-vector-兼容性)。

### 查询接口返回 HTTP 503

依次检查：

1. MongoDB 是否可连接。
2. `DASHSCOPE_API_KEY` 是否已加载到服务进程。
3. Zvec Studio 是否占用了生产集合锁。
4. 请求中的 `namespace` 和 `version` 是否与文档一致。
