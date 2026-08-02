"""RAG With Cold API Documents 的 FastAPI 应用入口。"""

import inspect
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from config import get_config
from src.dao.mongo import MongoBootstrap, MongoDatabase, QueryRecordDAO, YamlDocumentDAO
from src.dao.redis import RedisDatabase
from src.gateway import (
    gateway_router,
    graph_router,
    heatmap_router,
    knowledge_query_router,
    knowledge_update_router,
    rag_construction_router,
    yaml_import_router,
)
from src.knowledge import (
    KnowledgeUpdateService,
    OpenAIKnowledgeExtractor,
    ZvecKnowledgeIndexWriter,
)
from src.knowledge.graph.storage import MongoRelationGraphStore
from src.knowledge.mongo_repository import MongoKnowledgeRepository
from src.knowledge.query_service import build_knowledge_query_service
from src.service import (
    YamlImportService,
    build_agent_query_service,
    build_rag_construction_service,
)
from src.service.heatmap_counter import HeatmapCounter

logger = logging.getLogger(__name__)

_KNOWLEDGE_LLM_KEY_ENV = ("KNOWLEDGE_LLM_API_KEY", "OPENAI_API_KEY")
_KNOWLEDGE_LLM_BASE_URL_ENV = ("KNOWLEDGE_LLM_BASE_URL", "OPENAI_BASE_URL")
_KNOWLEDGE_LLM_MODEL_ENV = ("KNOWLEDGE_LLM_MODEL", "OPENAI_MODEL")
_DEFAULT_KNOWLEDGE_LLM_MODEL = "gpt-4o-mini"


def _first_non_empty_env(names: tuple[str, ...]) -> str | None:
    """在运行时读取第一个非空环境变量，不在模块导入阶段读取密钥。"""

    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def _build_knowledge_update_service(
    repository: MongoKnowledgeRepository,
    index_writer: ZvecKnowledgeIndexWriter,
) -> tuple[KnowledgeUpdateService | None, Any | None, str | None]:
    """按运行时 provider 配置构造 Knowledge 写入 Service。

    返回 ``(service, client, disabled_reason)``。没有 provider key 时不创建
    OpenAI client，应用仍可提供只读查询；写入路由会返回显式 503。
    """

    api_key = _first_non_empty_env(_KNOWLEDGE_LLM_KEY_ENV)
    if api_key is None:
        return None, None, "LLM provider API key is not configured"
    model = _first_non_empty_env(_KNOWLEDGE_LLM_MODEL_ENV) or _DEFAULT_KNOWLEDGE_LLM_MODEL
    base_url = _first_non_empty_env(_KNOWLEDGE_LLM_BASE_URL_ENV)
    try:
        # 延迟 import 和 client 构造，避免在 import 或无 provider 配置时读取密钥。
        from openai import AsyncOpenAI

        client_kwargs: dict[str, str] = {"api_key": api_key}
        if base_url is not None:
            client_kwargs["base_url"] = base_url
        client = AsyncOpenAI(**client_kwargs)
        extractor = OpenAIKnowledgeExtractor(client, model=model)
        return (
            KnowledgeUpdateService(repository, extractor, index_writer=index_writer),
            client,
            None,
        )
    except Exception as error:  # noqa: BLE001 - 启动可降级为显式 disabled。
        logger.warning(
            "knowledge_update.disabled provider_init_error=%s",
            type(error).__name__,
        )
        return None, None, "LLM provider initialization failed"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """连接 MongoDB、装配查询与导入 Service，并在关闭时释放连接。

    依赖降级矩阵：
    - Mongo 失败 → offline mock 模式，其他 service 也不装配。
    - Redis 失败 → 仅热力图接口降级，Mongo 服务照常工作（独立 try 块）。
    """
    mongo = MongoDatabase()
    redis_db = RedisDatabase()
    mongo_connected = False
    redis_connected = False

    # ---- Redis 独立初始化（Mongo 失败不影响热力图降级矩阵）----
    try:
        redis_connected = await redis_db.connect()
    except Exception as error:  # noqa: BLE001 - 启动期允许降级。
        logger.warning("redis.connect_failed_in_lifespan error_type=%s", type(error).__name__)
    heatmap_counter: HeatmapCounter | None = HeatmapCounter(redis_db) if redis_connected else None
    app.state.redis_db = redis_db
    app.state.heatmap_counter = heatmap_counter

    # ---- Mongo 初始化（失败时进入 offline mock 模式）----
    try:
        await mongo.connect()
        await MongoBootstrap(mongo).ensure_schema()
        app.state.mongo = mongo
        app.state.yaml_import_service = YamlImportService(YamlDocumentDAO(mongo))
        collection_name = get_config().zvec.default_collection
        app.state.agent_query_service = build_agent_query_service(
            QueryRecordDAO(mongo),
            collection_name=collection_name,
            heatmap_counter=heatmap_counter,
        )
        knowledge_repository = MongoKnowledgeRepository(mongo)
        await knowledge_repository.ensure_indexes()
        knowledge_index_writer = ZvecKnowledgeIndexWriter()
        app.state.knowledge_repository = knowledge_repository
        app.state.knowledge_index_writer = knowledge_index_writer
        # 构造图存储并保证索引存在；用于图召回
        graph_store = MongoRelationGraphStore(mongo)
        await graph_store.ensure_indexes()
        app.state.knowledge_graph_store = graph_store
        app.state.knowledge_query_service = build_knowledge_query_service(
            knowledge_repository,
            graph_store=graph_store,
        )
        (
            app.state.knowledge_update_service,
            app.state.knowledge_llm_client,
            disabled_reason,
        ) = _build_knowledge_update_service(knowledge_repository, knowledge_index_writer)
        app.state.knowledge_update_disabled_reason = disabled_reason
        if disabled_reason:
            logger.warning("knowledge_update.disabled reason=%s", disabled_reason)
        app.state.rag_construction_service = build_rag_construction_service()
        mongo_connected = True
    except Exception as error:  # noqa: BLE001 - 启动期允许无 mongo 模式。
        app.state.mongo = None
        logger.warning(
            "mongo_connect_failed reason=%s (running in offline/mock mode)", type(error).__name__
        )

    try:
        yield
    finally:
        llm_client = getattr(app.state, "knowledge_llm_client", None)
        if llm_client is not None:
            try:
                close_result = llm_client.close()
                if inspect.isawaitable(close_result):
                    await close_result
            except Exception as error:  # noqa: BLE001 - 关闭期允许降级。
                logger.warning("knowledge_update.llm_close_error type=%s", type(error).__name__)
        if redis_connected:
            await redis_db.close()
        if mongo_connected:
            await mongo.close()


app = FastAPI(
    title="RAG With Cold API Documents",
    description="面向代码 Agent 的 API 文档查询服务。",
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(gateway_router)
app.include_router(graph_router)
app.include_router(heatmap_router)
app.include_router(knowledge_query_router)
app.include_router(knowledge_update_router)
app.include_router(yaml_import_router)
app.include_router(rag_construction_router)

web_dir = Path("web")
if web_dir.exists():
    app.mount("/web", StaticFiles(directory="web", html=True), name="web")
