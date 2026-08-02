"""RAG With Cold API Documents 的 FastAPI 应用入口。"""

import inspect
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from config import get_config
from src.dao.mongo import MongoBootstrap, MongoDatabase, QueryRecordDAO, YamlDocumentDAO
from src.gateway import (
    gateway_router,
    knowledge_query_router,
    knowledge_update_router,
    rag_construction_router,
    retrieval_router,
    yaml_import_router,
)
from src.knowledge import (
    KnowledgeUpdateService,
    OpenAIKnowledgeExtractor,
    ZvecKnowledgeIndexWriter,
)
from src.knowledge.mongo_repository import MongoKnowledgeRepository
from src.knowledge.query_service import build_knowledge_query_service
from src.retrieve import (
    KnowledgeUpdateServiceScheduler,
    RagSourceReader,
    RetrievalPipelineService,
)
from src.service import (
    YamlImportService,
    build_agent_query_service,
    build_rag_construction_service,
)

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
    """连接 MongoDB、装配查询与导入 Service，并在关闭时释放连接。"""
    mongo = MongoDatabase()
    await mongo.connect()
    try:
        await MongoBootstrap(mongo).ensure_schema()
        app.state.mongo = mongo
        app.state.yaml_import_service = YamlImportService(YamlDocumentDAO(mongo))
        collection_name = get_config().zvec.default_collection
        app.state.agent_query_service = build_agent_query_service(
            QueryRecordDAO(mongo),
            collection_name=collection_name,
        )
        knowledge_repository = MongoKnowledgeRepository(mongo)
        await knowledge_repository.ensure_indexes()
        knowledge_index_writer = ZvecKnowledgeIndexWriter()
        app.state.knowledge_repository = knowledge_repository
        app.state.knowledge_index_writer = knowledge_index_writer
        app.state.knowledge_query_service = build_knowledge_query_service(
            knowledge_repository,
        )
        (
            app.state.knowledge_update_service,
            app.state.knowledge_llm_client,
            disabled_reason,
        ) = _build_knowledge_update_service(knowledge_repository, knowledge_index_writer)
        app.state.knowledge_update_disabled_reason = disabled_reason
        if disabled_reason:
            logger.warning("knowledge_update.disabled reason=%s", disabled_reason)
        update_scheduler = (
            KnowledgeUpdateServiceScheduler(app.state.knowledge_update_service)
            if app.state.knowledge_update_service is not None
            else None
        )
        app.state.retrieval_pipeline_service = RetrievalPipelineService(
            app.state.knowledge_query_service,
            RagSourceReader(collection_name),
            update_scheduler,
        )
        app.state.rag_construction_service = build_rag_construction_service()
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
        await mongo.close()


app = FastAPI(
    title="RAG With Cold API Documents",
    description="面向代码 Agent 的 API 文档查询服务。",
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(gateway_router)
app.include_router(knowledge_query_router)
app.include_router(knowledge_update_router)
app.include_router(retrieval_router)
app.include_router(yaml_import_router)
app.include_router(rag_construction_router)
