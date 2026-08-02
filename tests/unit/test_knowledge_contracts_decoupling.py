"""LLM Wiki 契约的独立性与旧字段兼容性测试。"""

from __future__ import annotations

from src.gateway.knowledge_query_schemas import KnowledgeQueryRequest
from src.knowledge.models import (
    KnowledgeDocumentInput,
    RagCollectionInput,
    SourceOrigin,
    SourcePart,
    WikiUpdateInput,
    stable_source_id,
)
from src.knowledge.query_contracts import QueryScope


def _document(**values: object) -> KnowledgeDocumentInput:
    payload: dict[str, object] = {
        "document_id": "doc-1",
        "wiki_id": "wiki:test",
        "namespace": "AscendC.API",
        "version": "910beta3",
        "content_hash": "sha256:document-content",
        "parts": (
            SourcePart(
                part_id="part-1",
                order=0,
                heading_path=("Overview",),
                content="The API computes a maximum reduction.",
            ),
        ),
    }
    payload.update(values)
    return KnowledgeDocumentInput(**payload)


def test_document_can_enter_wiki_without_rag_collection() -> None:
    """独立 Wiki 文档可以省略旧 RAG 标识，并保留来源描述。"""

    document = _document(
        source_origin=SourceOrigin(system="local-files", path="docs/api.md"),
        source_metadata={"import_job": "job-1"},
    )

    assert document.rag_collection_id == ""
    assert document.source_origin is not None
    assert document.source_origin.system == "local-files"
    assert document.source_metadata == {"import_job": "job-1"}
    assert document.source_id == stable_source_id(
        "wiki:test", "", "AscendC.API", "910beta3", "doc-1"
    )


def test_legacy_collection_identity_remains_stable() -> None:
    """旧输入的 Source ID 仍使用原有哈希格式。"""

    document = _document(rag_collection_id="rag:legacy")

    assert document.source_id == stable_source_id(
        "wiki:test", "rag:legacy", "AscendC.API", "910beta3", "doc-1"
    )


def test_legacy_batch_shape_accepts_collectionless_documents() -> None:
    """历史 RagCollectionInput 外壳可以承载没有 RAG 标识的 Wiki 文档。"""

    document = _document()
    request = WikiUpdateInput(
        wiki_id="wiki:test",
        rag_collections=(RagCollectionInput(documents=(document,)),),
    )

    assert request.documents == (document,)
    assert request.rag_collections[0].rag_collection_id == ""


def test_query_scope_and_gateway_request_make_rag_filter_optional() -> None:
    """省略 RAG 集合时，查询契约仍要求 Wiki/namespace/version。"""

    scope = QueryScope(
        wiki_id="wiki:test",
        namespace="AscendC.API",
        version="910beta3",
    )
    request = KnowledgeQueryRequest(
        query="maximum reduction",
        wiki_id="wiki:test",
        namespace="AscendC.API",
        version="910beta3",
    )

    assert scope.rag_collection_ids == ()
    assert request.rag_collection_ids == ()
