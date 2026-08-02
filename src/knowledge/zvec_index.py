"""Knowledge Wiki 的独立 Zvec 派生索引写入器。

底层 API RAG 的 collection 与 Knowledge Wiki 的 collection 绝不共用 schema。这里
只为已发布的 Artifact Revision 建索引；读侧的召回、rerank 和上下文构造属于模块二。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable

import zvec

from config import get_config
from src.dao.emb.director import CollectionSession
from src.dao.emb.embedder import Embedder, build_embedder
from src.dao.emb.schema import FIELD_DENSE_EMBEDDING, FIELD_SPARSE_EMBEDDING
from src.knowledge.models import ArtifactRevision
from src.knowledge.reindex import RebuildResult

logger = logging.getLogger(__name__)

FIELD_ARTIFACT_ID = "artifact_id"
FIELD_ARTIFACT_REVISION_ID = "artifact_revision_id"
FIELD_ARTIFACT_TYPE = "artifact_type"
FIELD_WIKI_ID = "wiki_id"
FIELD_NAMESPACE = "namespace"
FIELD_VERSION = "version"
FIELD_CANONICAL_NAME = "canonical_name"
FIELD_ALIASES = "aliases"
FIELD_STATUS = "status"
FIELD_SOURCE_IDS = "source_ids"
FIELD_CREATED_AT = "created_at"
FIELD_TITLE = "title"
FIELD_SUMMARY = "summary"
FIELD_CLAIMS_JSON = "claims_json"
FIELD_RELATIONS_JSON = "relations_json"
FIELD_PROVENANCE_JSON = "provenance_json"

DEFAULT_KNOWLEDGE_COLLECTION = "knowledge_wiki_v1"


def _escape_filter(value: str) -> str:
    """转义 Zvec 过滤表达式中的字符串字面量。"""

    return value.replace("\\", "\\\\").replace("'", "\\'")


def _knowledge_scope_filter(
    *,
    wiki_id: str | None = None,
    namespace: str | None = None,
    version: str | None = None,
) -> str:
    """构造索引清理/校验共用的精确 Scope 过滤器。"""

    clauses = [f"{FIELD_ARTIFACT_ID} != ''"]
    for field, value in (
        (FIELD_WIKI_ID, wiki_id),
        (FIELD_NAMESPACE, namespace),
        (FIELD_VERSION, version),
    ):
        if value is not None:
            clauses.append(f"{field} = '{_escape_filter(value)}'")
    return " AND ".join(clauses)


def get_knowledge_collection_schema(
    name: str = DEFAULT_KNOWLEDGE_COLLECTION,
) -> zvec.CollectionSchema:
    """构造专用于 Knowledge Artifact 的 Zvec schema。

    collection 名包含 schema 主版本；不支持的 schema 变更应新建 collection 并
    重新建立派生索引，不能冒险迁移现有的 string/vector 列。
    """

    config = get_config()
    dimension = (
        config.embedder.bailian.dimension
        if config.embedder.type == "bailian"
        else config.embedder.local.dimension
    )
    indexed_string = lambda field: zvec.FieldSchema(
        name=field,
        data_type=zvec.DataType.STRING,
        index_param=zvec.InvertIndexParam(),
    )
    indexed_array = lambda field: zvec.FieldSchema(
        name=field,
        data_type=zvec.DataType.ARRAY_STRING,
        index_param=zvec.InvertIndexParam(enable_range_optimization=False),
    )
    return zvec.CollectionSchema(
        name=name,
        fields=[
            indexed_string(FIELD_ARTIFACT_ID),
            indexed_string(FIELD_ARTIFACT_REVISION_ID),
            indexed_string(FIELD_ARTIFACT_TYPE),
            indexed_string(FIELD_WIKI_ID),
            indexed_string(FIELD_NAMESPACE),
            indexed_string(FIELD_VERSION),
            indexed_string(FIELD_CANONICAL_NAME),
            indexed_array(FIELD_ALIASES),
            indexed_string(FIELD_STATUS),
            indexed_array(FIELD_SOURCE_IDS),
            zvec.FieldSchema(
                name=FIELD_CREATED_AT,
                data_type=zvec.DataType.INT64,
                index_param=zvec.InvertIndexParam(enable_range_optimization=True),
            ),
            zvec.FieldSchema(name=FIELD_TITLE, data_type=zvec.DataType.STRING),
            zvec.FieldSchema(name=FIELD_SUMMARY, data_type=zvec.DataType.STRING),
            zvec.FieldSchema(name=FIELD_CLAIMS_JSON, data_type=zvec.DataType.STRING),
            zvec.FieldSchema(name=FIELD_RELATIONS_JSON, data_type=zvec.DataType.STRING),
            zvec.FieldSchema(name=FIELD_PROVENANCE_JSON, data_type=zvec.DataType.STRING),
        ],
        vectors=[
            zvec.VectorSchema(
                name=FIELD_DENSE_EMBEDDING,
                data_type=zvec.DataType.VECTOR_FP32,
                dimension=dimension,
                index_param=zvec.HnswIndexParam(metric_type=zvec.MetricType.COSINE),
            ),
            zvec.VectorSchema(
                name=FIELD_SPARSE_EMBEDDING,
                data_type=zvec.DataType.SPARSE_VECTOR_FP32,
                index_param=zvec.HnswIndexParam(metric_type=zvec.MetricType.IP),
            ),
        ],
    )


def artifact_index_text(artifact: ArtifactRevision) -> str:
    """生成同时适合 dense 与 sparse 写入的稳定 Artifact 摘要文本。"""

    draft = artifact.draft
    claims = "\n".join(claim.text for claim in draft.claims)
    aliases = " ".join(draft.aliases)
    return "\n".join(
        value
        for value in (
            draft.canonical_name,
            aliases,
            draft.title,
            draft.summary,
            claims,
        )
        if value
    )


def artifact_to_zvec_doc(artifact: ArtifactRevision, embedder: Embedder) -> zvec.Doc:
    """将不可变 Artifact Revision 投影为可重建的 Zvec 文档。"""

    draft = artifact.draft
    text = artifact_index_text(artifact)
    if not text:
        raise ValueError(f"Artifact {artifact.artifact_revision_id} 没有可索引文本")
    claims = [claim.model_dump(mode="json") for claim in draft.claims]
    relations = [relation.model_dump(mode="json") for relation in draft.related_artifacts]
    provenance = {
        "source_revision_id": artifact.source_revision_id,
        "wiki_id": artifact.wiki_id,
        "source_ids": list(artifact.source_ids),
        "extractor_version": artifact.extractor_version,
        "prompt_version": artifact.prompt_version,
        "model": artifact.model,
        "schema_version": artifact.schema_version,
    }
    return zvec.Doc(
        id=artifact.artifact_id,
        fields={
            FIELD_ARTIFACT_ID: artifact.artifact_id,
            FIELD_ARTIFACT_REVISION_ID: artifact.artifact_revision_id,
            FIELD_ARTIFACT_TYPE: draft.artifact_type.value,
            FIELD_WIKI_ID: artifact.wiki_id,
            FIELD_NAMESPACE: draft.namespace,
            FIELD_VERSION: draft.version,
            FIELD_CANONICAL_NAME: draft.canonical_name,
            FIELD_ALIASES: list(draft.aliases),
            FIELD_STATUS: artifact.status.value,
            FIELD_SOURCE_IDS: list(artifact.source_ids),
            FIELD_CREATED_AT: int(artifact.created_at.timestamp()),
            FIELD_TITLE: draft.title,
            FIELD_SUMMARY: draft.summary,
            FIELD_CLAIMS_JSON: json.dumps(claims, ensure_ascii=False, sort_keys=True),
            FIELD_RELATIONS_JSON: json.dumps(relations, ensure_ascii=False, sort_keys=True),
            FIELD_PROVENANCE_JSON: json.dumps(provenance, ensure_ascii=False, sort_keys=True),
        },
        vectors={
            FIELD_DENSE_EMBEDDING: embedder.embed_dense(text, mode="document"),
            FIELD_SPARSE_EMBEDDING: embedder.embed_sparse(text, mode="document"),
        },
    )


class ZvecKnowledgeIndexWriter:
    """为正式发布的 Artifact 建立独立的 dense+sparse 物化索引。"""

    def __init__(
        self,
        *,
        collection_name: str = DEFAULT_KNOWLEDGE_COLLECTION,
        embedder_factory: Callable[[], Embedder] = build_embedder,
    ) -> None:
        self._collection_name = collection_name
        self._embedder_factory = embedder_factory

    async def upsert(self, artifacts: tuple[ArtifactRevision, ...]) -> None:
        """串行写入 Zvec；阻塞的 embedding/IO 在线程中执行。"""

        if not artifacts:
            return
        await asyncio.to_thread(self._upsert_sync, artifacts)

    async def rebuild(
        self,
        artifacts: tuple[ArtifactRevision, ...],
        *,
        wiki_id: str | None = None,
        namespace: str | None = None,
        version: str | None = None,
    ) -> RebuildResult:
        """按正式 Catalog 快照重建指定 Scope 的派生索引。

        Zvec 0.6 提供 ``delete_by_filter``，所以可以先清理当前 Scope，再
        重新写入 active Revision。删除只发生在 Knowledge 自己的 collection，
        不会触碰底层 API RAG collection；如果 embedding 或写入失败，Mongo
        正式知识仍然保持不变，调用方会得到失败结果并可再次重建。
        """

        return await asyncio.to_thread(
            self._rebuild_sync,
            artifacts,
            wiki_id=wiki_id,
            namespace=namespace,
            version=version,
        )

    def _rebuild_sync(
        self,
        artifacts: tuple[ArtifactRevision, ...],
        *,
        wiki_id: str | None,
        namespace: str | None,
        version: str | None,
    ) -> RebuildResult:
        """同步执行 Scope 清理和完整 Artifact 投影。"""

        schema = get_knowledge_collection_schema(self._collection_name)
        indexed_count = 0
        failed: list[str] = []
        embedder: Embedder | None = None
        try:
            with CollectionSession(self._collection_name, schema=schema) as collection:
                collection.delete_by_filter(
                    _knowledge_scope_filter(
                        wiki_id=wiki_id,
                        namespace=namespace,
                        version=version,
                    )
                )
                if not artifacts:
                    return RebuildResult(indexed_count=0)
                embedder = self._embedder_factory()
                texts = [artifact_index_text(artifact) for artifact in artifacts]
                embedder.fit_sparse(texts)
                for artifact in artifacts:
                    try:
                        collection.upsert(artifact_to_zvec_doc(artifact, embedder))
                        indexed_count += 1
                    except Exception as e:
                        failed.append(artifact.artifact_id)
                        logger.warning(
                            "rebuild.upsert_failed artifact_id=%s error=%s",
                            artifact.artifact_id,
                            e,
                        )
                collection.flush()
        finally:
            if embedder is not None:
                embedder.close()
        return RebuildResult(
            indexed_count=indexed_count,
            failed_artifact_ids=tuple(failed),
        )

    async def check_consistency(
        self,
        artifacts: tuple[ArtifactRevision, ...],
        *,
        wiki_id: str | None = None,
        namespace: str | None = None,
        version: str | None = None,
    ) -> dict[str, object]:
        """检查期望的 Artifact ID 是否都存在于 Zvec。

        Zvec 0.6 没有稳定的全量 ID 枚举接口，因此无法在这里可靠统计 Scope
        外的孤儿文档；返回 ``orphan_count=None``，由维护工具在支持枚举的
        后端上补充。缺失 ID 会明确列出，便于下一次重建。
        """

        del wiki_id, namespace, version
        return await asyncio.to_thread(self._check_consistency_sync, artifacts)

    def _check_consistency_sync(self, artifacts: tuple[ArtifactRevision, ...]) -> dict[str, object]:
        """同步检查预期 ID；只读打开失败时交给上层记录为 warning。"""

        expected_ids = tuple(dict.fromkeys(artifact.artifact_id for artifact in artifacts))
        try:
            with CollectionSession(self._collection_name, read_only=True) as collection:
                if not expected_ids:
                    return {
                        "expected_count": 0,
                        "present_count": 0,
                        "missing_artifact_ids": (),
                        "orphan_count": None,
                    }
                present = collection.fetch(ids=list(expected_ids), include_vector=False)
        except Exception as error:  # noqa: BLE001 - consistency is best effort
            return {
                "expected_count": len(expected_ids),
                "present_count": 0,
                "missing_artifact_ids": expected_ids,
                "orphan_count": None,
                "error": f"{type(error).__name__}: {error}",
            }
        present_ids = set(present or {})
        return {
            "expected_count": len(expected_ids),
            "present_count": len(present_ids),
            "missing_artifact_ids": tuple(sorted(set(expected_ids) - present_ids)),
            "orphan_count": None,
        }

    def _upsert_sync(self, artifacts: tuple[ArtifactRevision, ...]) -> None:
        """建立 sparse corpus 后生成向量并 upsert，始终释放 embedder 资源。"""

        embedder = self._embedder_factory()
        try:
            texts = [artifact_index_text(artifact) for artifact in artifacts]
            embedder.fit_sparse(texts)
            schema = get_knowledge_collection_schema(self._collection_name)
            with CollectionSession(self._collection_name, schema=schema) as collection:
                for artifact in artifacts:
                    collection.upsert(artifact_to_zvec_doc(artifact, embedder))
        finally:
            embedder.close()
