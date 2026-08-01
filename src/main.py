"""RAG With Cold API Documents 的 FastAPI 应用入口。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from config import get_config
from src.dao.mongo import MongoBootstrap, MongoDatabase, QueryRecordDAO, YamlDocumentDAO
from src.gateway import gateway_router, rag_construction_router, yaml_import_router
from src.service import (
    YamlImportService,
    build_agent_query_service,
    build_rag_construction_service,
)


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
        app.state.rag_construction_service = build_rag_construction_service()
        yield
    finally:
        await mongo.close()


app = FastAPI(
    title="RAG With Cold API Documents",
    description="面向代码 Agent 的 API 文档查询服务。",
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(gateway_router)
app.include_router(yaml_import_router)
app.include_router(rag_construction_router)
