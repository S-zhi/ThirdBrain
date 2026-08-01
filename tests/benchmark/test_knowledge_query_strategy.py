"""Knowledge Query V1 golden case 的可执行策略回归。"""

from __future__ import annotations

import json
from pathlib import Path

from src.knowledge import (
    ArtifactKind,
    Confidence,
    KnowledgeItem,
    QueryBudget,
    QueryEvidenceRef,
    QueryKnowledgeOptions,
    QueryScope,
    ReaderSearchResult,
    RetrievalChannel,
    RetrievalHit,
)
from src.knowledge.query_service import KnowledgeQueryService
from src.knowledge.readers import EmptyRelationReader


class BenchmarkArtifactReader:
    """Golden case 使用的确定性 Knowledge Artifact 语料。"""

    async def search(self, query, options, *, limit):
        del limit
        ids = (
            ["AscendC.API.910beta3.Barrier"]
            if query != "缓存一致性"
            else [f"AscendC.API.910beta3.Cache{index}" for index in range(5)]
        )
        hits = []
        for index, item_id in enumerate(ids):
            item = KnowledgeItem(
                id=item_id,
                kind=ArtifactKind.SOURCE,
                wiki_id=options.scope.wiki_id,
                rag_collection_ids=(),
                namespace=options.scope.namespace,
                version=options.scope.version,
                title=item_id.rsplit(".", 1)[-1],
                summary="benchmark source",
                confidence=Confidence.HIGH,
                provenance=(
                    QueryEvidenceRef(
                        wiki_id=options.scope.wiki_id,
                        rag_collection_id="",
                        document_id=item_id,
                        part_id=f"part-{index}",
                        content_hash="sha256:benchmark",
                        version=options.scope.version,
                    ),
                ),
            )
            channel = RetrievalChannel.EXACT if query == "Barrier" else RetrievalChannel.DENSE
            hits.append(RetrievalHit(channel=channel, ranking="benchmark", item=item))
        # 伪造一个大小写错误 namespace，证明运行时硬过滤真正生效。
        if query == "Barrier":
            wrong = hits[0].item.model_copy(
                update={
                    "id": "ascendc.api.910beta3.Barrier",
                    "namespace": "ascendc.api.910beta3",
                }
            )
            hits.append(RetrievalHit(channel=RetrievalChannel.DENSE, item=wrong))
        return ReaderSearchResult(hits=tuple(hits))


async def test_knowledge_query_v1_cases_execute_core_regressions() -> None:
    """Golden cases 必须执行真实编排，而不是只校验 JSON 字段存在。"""
    path = Path(__file__).parent / "cases" / "knowledge_query_v1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    service = KnowledgeQueryService(
        BenchmarkArtifactReader(),
        EmptyRelationReader(),
    )

    assert payload["schema_version"] == "1.0"
    for case in payload["cases"]:
        result = await service.query_knowledge(
            case["query"],
            QueryKnowledgeOptions(
                scope=QueryScope(
                    wiki_id=case["wiki_id"],
                    rag_collection_ids=tuple(case.get("rag_collection_ids", ())),
                    namespace=case["namespace"],
                    version=case["version"],
                ),
                budget=QueryBudget.MICRO,
            ),
        )
        result_ids = {item.id for item in result.knowledge_hits}
        assert result.source_hits == ()
        assert result.cache_misses == ()
        assert result.enrichment_requests == ()
        assert set(case.get("expected_ids", ())).issubset(result_ids)
        assert result_ids.isdisjoint(case.get("forbidden_ids", ()))
        if case.get("require_provenance"):
            assert result.recall_capsule.items
            assert all(item.provenance for item in result.recall_capsule.items)
        if max_items := case.get("max_capsule_items"):
            assert result.recall_capsule.count <= max_items
