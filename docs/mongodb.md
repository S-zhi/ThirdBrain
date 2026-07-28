# MongoDB 数据层快速配置

> 状态：实施完成（数据层 + 启动初始化），Service 编排层后续任务单独推进
> 模块路径：`src/dao/mongo/`
> 启动入口：`service/main.py`（`service/lifespan.py` 负责连接 + 初始化）

本数据层实现两张 Collection：

| Collection | 作用 | 关键约束 |
|---|---|---|
| `lig_update_records` | 每次 LIG 更新任务的全量执行记录 | `record_id` 唯一、`idempotency_key` 唯一 |
| `lig_text_states` | 每个 `(namespace, text_id)` 的当前状态 | `(namespace, text_id)` 唯一、`revision` 乐观锁 |

DAO 只做 CRUD、字段白名单校验、乐观锁与游标分页；版本号计算、状态机迁移、LIG 构建、Diff 计算、嵌入生成等业务逻辑放在 Service 层。

---

## 1. 本地启动 MongoDB

任选一种方式：

### 方式 A：Docker（推荐开发环境）

```bash
docker run -d \
  --name rag-mongodb \
  -p 27017:27017 \
  -v rag-mongodb-data:/data/db \
  mongo:8.0
```

### 方式 B：复用仓库已下载的 mongod 8.3.7

仓库根目录已经包含 `mongodb-macos-aarch64--8.3.7/bin/mongod`，可执行：

```bash
mkdir -p tmp/mongo-data
./mongodb-macos-aarch64--8.3.7/bin/mongod \
  --dbpath tmp/mongo-data \
  --bind_ip 127.0.0.1 \
  --port 27017
```

### 方式 C：Atlas / Replica Set

把 URI 改为 `mongodb+srv://...`，并在 Atlas 控制台放通 IP。`RAG_MONGO_USE_TRANSACTIONS=true` 仅当集群支持事务时打开。

---

## 2. 配置环境变量

```bash
cp .env.example .env
```

本地最小 `.env`（Standalone MongoDB）：

```ini
RAG_MONGO_URI=mongodb://127.0.0.1:27017
RAG_MONGO_DATABASE=rag_cold_api
RAG_MONGO_INIT_MODE=auto
RAG_MONGO_USE_TRANSACTIONS=false
```

> **安全要求**：
> - `.env` 已在 `.gitignore`；仓库只提交 `.env.example`。
> - `.env.example` 不放真实账号密码。
> - Atlas 用户通过环境变量传入 `mongodb+srv://user:pass@host`。
> - 日志绝不打印 URI、密码、完整 `source_markdown`、diff 正文或 embedding 向量。

`RAG_MONGO_INIT_MODE` 取值：

| 值 | 行为 |
|---|---|
| `auto` | 默认；创建缺失的 Collection 和索引 |
| `validate` | 只检查；缺失时启动失败 |
| `off` | 仅连接，不检查 Schema |

---

## 3. 安装依赖

```bash
uv sync
```

如果还要跑测试：

```bash
uv sync --group dev
```

---

## 4. 启动应用

```bash
# 沿用项目已有启动命令（AGENTS.md §5）：
uv run uvicorn <你的入口模块>:<app> --reload
```

`src/dao/mongo/` 不耦合 Web 框架，**调用方自己拼装启动/关闭逻辑**。FastAPI 接入示例（4 行）：

```python
# service/main.py（由后续任务创建）
from contextlib import asynccontextmanager
from fastapi import FastAPI

from src.dao.mongo import (
    MongoDatabase, MongoBootstrap, LIGUpdateRecordDAO, LIGTextStateDAO,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    mongo = MongoDatabase()
    bootstrap = MongoBootstrap(mongo)
    await mongo.connect()
    await bootstrap.ensure_schema()
    app.state.mongo, app.state.record_dao, app.state.state_dao = (
        mongo, LIGUpdateRecordDAO(mongo), LIGTextStateDAO(mongo),
    )
    try:
        yield
    finally:
        await mongo.close()


app = FastAPI(lifespan=lifespan)
```

DAO 自身不依赖 FastAPI；同样的 `connect / ensure_schema / close` 三步也适合非 FastAPI 进程（例如一次性 CLI 工具）。

---

## 5. 首次启动预期行为

1. `connect()`：建立 `AsyncMongoClient` 并 `ping` 验证连接。
2. `MongoBootstrap.ensure_schema()`：
   - 数据库不存在则 `db.create_collection()`。
   - 集合不存在则 `create_collection(lig_update_records)` / `create_collection(lig_text_states)`。
   - 全部 13 个固定名称索引幂等创建。
3. 应用开始接收请求，`/healthz`、`/readyz` 可用。

## 6. 重复启动预期行为

1. `connect()`：直接复用 `AsyncMongoClient`。
2. `MongoBootstrap.ensure_schema()`：
   - 集合已存在 → 跳过 `create_collection`。
   - 索引名已存在 → 跳过 `create_index`。
   - 同名索引配置不一致 → **启动失败**，提示需要显式迁移（不自动删除重建）。
3. 已有数据不被删除或修改。

---

## 7. 模块结构

```
src/dao/mongo/
├── __init__.py
├── settings.py              # pydantic-settings: RAG_MONGO_*
├── exceptions.py            # DAOError / 子类
├── enums.py                 # UpdateOperation / UpdateStatus / LifecycleState / ...
├── models.py                # LIGUpdateRecord / LIGTextState / Patch / Query
├── database.py              # MongoDatabase 生命周期
├── _tracing.py              # 异常映射 / 结构化日志 / 白名单校验
├── lig_update_record_dao.py
├── lig_text_state_dao.py
└── bootstrap.py             # MongoBootstrap.ensure_schema()
```

入口导出见 `src/dao/mongo/__init__.py`。

---

## 8. 关键约定

- **PyMongo Async**（`AsyncMongoClient`），不使用 Motor。
- **乐观锁**：`update` 必须传 `expected_status`（记录）或 `expected_revision`（状态）；不匹配抛 `DAOConcurrentUpdateError` / `DAONotFoundError`。
- **字段白名单**：`update` 只接受白名单字段；超白名单抛 `DAOValidationError`。
- **唯一约束**：`record_id`、`idempotency_key`、`(namespace, text_id)`；重复抛 `DAOAlreadyExistsError`。
- **游标分页**：`list_by_text` / `list`，基于 `(created_at|updated_at, _id)`，默认 50，最大 200。
- **不吞原始异常**：日志可记录 `error_type`，但不打印 URI / 密码 / 完整正文。
- **事务**：`RAG_MONGO_USE_TRANSACTIONS=false` 时不依赖事务；Service 用「先更新状态 → 再写结果」+ `current_record_id` / `pending_record_id` 对账。

---

## 9. DAO 错误体系

```
DAOError
├── DAOAlreadyExistsError     # DuplicateKeyError
├── DAONotFoundError          # get() 返回 None；update matched=0 + 文档不存在
├── DAOConcurrentUpdateError  # revision/expected_status 不匹配
├── DAOValidationError        # 非法 Patch、超白名单
└── DAOUnavailableError       # ServerSelectionTimeoutError / ConnectionFailure
```

Service / Router 通过捕获 `DAOError` 兜底，区分类型决定 HTTP 状态码与重试策略。

---

## 10. 验证

### 10.1 健康检查

```bash
curl http://127.0.0.1:8000/healthz
# {"status":"ok"}

curl http://127.0.0.1:8000/readyz
# {"status":"ok","mongo":{"database":"rag_cold_api", ...}}
```

### 10.2 单元测试

```bash
uv run pytest tests/unit -k mongo -v
```

### 10.3 手动冒烟

```bash
uv run python tmp/mongo_smoke.py
```

（`tmp/mongo_smoke.py` 是单文件冒烟脚本，演示 connect → ensure_schema → 写一条记录 → 读回 → 删除的完整流程。）

---

## 11. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `MongoDB server selection timed out` | mongod 未启动 / URI 写错 / 防火墙 | `mongosh` 验证连通性 |
| `IndexOptionsConflict` | 同名索引 key 或 options 与现状不一致 | 不要自动 drop；先 `db.collection.dropIndex(name)`，再 `uv run` |
| `validate mode: ... missing indexes` | `RAG_MONGO_INIT_MODE=validate` 但 schema 缺失 | 临时切到 `auto` 补齐，或手写迁移脚本 |
| `duplicate key error` | 业务幂等键冲突 | 由 Service 决定重试或抛错；DAO 只翻译为 `DAOAlreadyExistsError` |
