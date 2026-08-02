"""Knowledge Wiki 审核和操作状态的领域层测试。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from uuid import uuid4

import pytest

from src.knowledge.models import (
    ArtifactDraft,
    ArtifactRevision,
    ArtifactStatus,
    ArtifactType,
    Confidence,
    EvidenceRef,
    KnowledgeClaim,
    KnowledgeDocumentInput,
    SourcePart,
    SourceRevision,
    utc_now,
)
from src.knowledge.operations import (
    InMemoryOperationStore,
    ReviewDecision,
    ReviewOperationStatus,
)
from src.knowledge.repository import InMemoryKnowledgeIndexWriter
from src.knowledge.review_service import KnowledgeReviewService


def _document(*, content: str = "DataMove must precede Compute.") -> KnowledgeDocumentInput:
    return KnowledgeDocumentInput(
        document_id="doc-review",
        wiki_id="wiki-review",
        rag_collection_id="source-import",
        namespace="AscendC.910beta3",
        version="910beta3",
        content_hash="a" * 64,
        source_path="API参考/DataMove.md",
        parts=(
            SourcePart(
                part_id="part-1",
                order=1,
                heading_path=("DataMove",),
                content=content,
            ),
        ),
    )


def _pending(document: KnowledgeDocumentInput, *, quote_hint: str = "DataMove") -> ArtifactRevision:
    draft = ArtifactDraft(
        artifact_type=ArtifactType.ENTITY,
        wiki_id=document.wiki_id,
        namespace=document.namespace,
        version=document.version,
        canonical_name="DataMove",
        title="DataMove",
        summary="A reviewed API entity.",
        claims=(
            KnowledgeClaim(
                text="DataMove has an ordering constraint.",
                confidence=Confidence.HIGH,
                evidence=(
                    EvidenceRef(
                        document_id=document.document_id,
                        rag_collection_id=document.rag_collection_id,
                        part_id="part-1",
                        content_hash=document.parts[0].content_hash,
                        path=document.source_path,
                        quote_hint=quote_hint,
                    ),
                ),
            ),
        ),
    )
    source = SourceRevision(
        source_revision_id="sr-review",
        source_id=document.source_id,
        wiki_id=document.wiki_id,
        document=document,
        revision_number=1,
        compiler_fingerprint="b" * 64,
        created_at=utc_now(),
    )
    del source
    return ArtifactRevision(
        artifact_revision_id=f"ar-{uuid4()}",
        artifact_id=draft.artifact_id,
        wiki_id=document.wiki_id,
        source_revision_id="sr-review",
        revision_number=1,
        status=ArtifactStatus.PENDING_REVIEW,
        draft=draft,
        source_ids=(document.source_id,),
        extractor_version="v1",
        prompt_version="v1",
        model="model-v1",
        schema_version="1",
        created_at=utc_now(),
    )


@dataclass
class FakeReviewRepository:
    artifact: ArtifactRevision | None
    source: SourceRevision | None

    def __post_init__(self) -> None:
        self.decisions: list[tuple[str, ReviewDecision]] = []

    async def list_pending_artifacts(
        self,
        wiki_id: str | None = None,
        namespace: str | None = None,
        version: str | None = None,
    ) -> tuple[ArtifactRevision, ...]:
        artifact = self.artifact
        if artifact is None or artifact.status != ArtifactStatus.PENDING_REVIEW:
            return ()
        if wiki_id is not None and artifact.wiki_id != wiki_id:
            return ()
        if namespace is not None and artifact.draft.namespace != namespace:
            return ()
        if version is not None and artifact.draft.version != version:
            return ()
        return (artifact.model_copy(deep=True),)

    async def get_review_artifact(self, artifact_revision_id: str) -> ArtifactRevision | None:
        if self.artifact and self.artifact.artifact_revision_id == artifact_revision_id:
            return self.artifact.model_copy(deep=True)
        return None

    async def get_source_revision(self, source_revision_id: str) -> SourceRevision | None:
        if self.source and self.source.source_revision_id == source_revision_id:
            return self.source.model_copy(deep=True)
        return None

    async def apply_review_decision(
        self,
        artifact_revision_id: str,
        decision: ReviewDecision,
        *,
        actor: str,
        operation_id: str,
    ) -> ArtifactRevision:
        del actor, operation_id
        assert self.artifact is not None
        assert self.artifact.artifact_revision_id == artifact_revision_id
        self.decisions.append((artifact_revision_id, decision))
        status = (
            ArtifactStatus.ACTIVE if decision == ReviewDecision.APPROVE else ArtifactStatus.ARCHIVED
        )
        self.artifact = self.artifact.model_copy(update={"status": status}, deep=True)
        return self.artifact.model_copy(deep=True)


def _repository(*, quote_hint: str = "DataMove") -> FakeReviewRepository:
    document = _document()
    source = SourceRevision(
        source_revision_id="sr-review",
        source_id=document.source_id,
        wiki_id=document.wiki_id,
        document=document,
        revision_number=1,
        compiler_fingerprint="b" * 64,
        created_at=utc_now(),
    )
    return FakeReviewRepository(_pending(document, quote_hint=quote_hint), source)


@pytest.mark.asyncio
async def test_list_pending_reviews_applies_exact_scope_filters() -> None:
    repository = _repository()
    service = KnowledgeReviewService(repository)

    assert len(await service.list_pending_reviews(wiki_id="wiki-review")) == 1
    assert await service.list_pending_reviews(wiki_id="other-wiki") == ()


@pytest.mark.asyncio
async def test_approve_revalidates_evidence_and_records_operation() -> None:
    repository = _repository()
    operation_store = InMemoryOperationStore()
    index = InMemoryKnowledgeIndexWriter()
    service = KnowledgeReviewService(
        repository,
        operation_store=operation_store,
        index_writer=index,
    )

    artifact = cast(ArtifactRevision, repository.artifact)
    operation = await service.approve(artifact.artifact_revision_id, actor="reviewer@example.com")

    assert operation.decision == ReviewDecision.APPROVE
    assert operation.status == ReviewOperationStatus.COMPLETED
    assert operation.validation.passed
    assert operation.next_actions == ()
    assert repository.decisions == [(artifact.artifact_revision_id, ReviewDecision.APPROVE)]
    assert len(index.upserts) == 1
    stored = await service.get_operation(operation.operation_id)
    assert stored is not None
    assert stored.status == ReviewOperationStatus.COMPLETED


@pytest.mark.asyncio
async def test_invalid_evidence_blocks_both_approve_and_reject() -> None:
    repository = _repository(quote_hint="not present")
    service = KnowledgeReviewService(repository)
    artifact = cast(ArtifactRevision, repository.artifact)

    approved = await service.approve(artifact.artifact_revision_id, actor="reviewer")
    rejected = await service.reject(artifact.artifact_revision_id, actor="reviewer")

    assert approved.status == ReviewOperationStatus.FAILED
    assert rejected.status == ReviewOperationStatus.FAILED
    assert approved.validation.issues[0].code == "EVIDENCE_QUOTE_NOT_FOUND"
    assert rejected.validation.issues[0].code == "EVIDENCE_QUOTE_NOT_FOUND"
    assert repository.decisions == []


@pytest.mark.asyncio
async def test_approve_reports_partial_when_derived_index_fails() -> None:
    repository = _repository()
    service = KnowledgeReviewService(
        repository,
        index_writer=InMemoryKnowledgeIndexWriter(fail=True),
    )
    artifact = cast(ArtifactRevision, repository.artifact)

    operation = await service.approve(artifact.artifact_revision_id, actor="reviewer")

    assert operation.status == ReviewOperationStatus.PARTIAL
    assert operation.next_actions == ("rebuild_knowledge_indexes",)
    assert repository.decisions == [(artifact.artifact_revision_id, ReviewDecision.APPROVE)]


@pytest.mark.asyncio
async def test_reject_leaves_no_index_work_and_exits_pending_state() -> None:
    repository = _repository()
    service = KnowledgeReviewService(repository)
    artifact = cast(ArtifactRevision, repository.artifact)

    operation = await service.reject(artifact.artifact_revision_id, actor="reviewer")

    assert operation.status == ReviewOperationStatus.COMPLETED
    assert operation.decision == ReviewDecision.REJECT
    assert repository.artifact is not None
    assert repository.artifact.status == ArtifactStatus.ARCHIVED
