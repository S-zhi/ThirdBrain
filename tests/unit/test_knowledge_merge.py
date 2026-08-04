"""Unit tests for ConservativeMergePlanner claim merging behavior."""

from __future__ import annotations

import pytest

from src.knowledge.merge import _merge_claims
from src.knowledge.models import Confidence, EvidenceRef, KnowledgeClaim


def _make_evidence(
    *,
    document_id: str = "doc-1",
    rag_collection_id: str = "coll-1",
    part_id: str = "part-1",
    content_hash: str = "a" * 64,
    quote_hint: str = "hint-1",
    char_start: int | None = None,
    char_end: int | None = None,
) -> EvidenceRef:
    """Helper to create an EvidenceRef."""
    return EvidenceRef(
        document_id=document_id,
        rag_collection_id=rag_collection_id,
        part_id=part_id,
        content_hash=content_hash,
        path=f"path/{document_id}.md",
        quote_hint=quote_hint,
        char_start=char_start,
        char_end=char_end,
    )


def test_merge_claims_same_text_different_evidence() -> None:
    """单元测试: 同 text + 不同 evidence → 1 条 claim, evidence 合并并且去重。"""
    evidence_1 = _make_evidence(document_id="doc-1", content_hash="a" * 64)
    evidence_2 = _make_evidence(document_id="doc-2", content_hash="b" * 64)

    current = (
        KnowledgeClaim(
            text="X is true",
            confidence=Confidence.HIGH,
            evidence=(evidence_1,),
        ),
    )
    incoming = (
        KnowledgeClaim(
            text="X is true",
            confidence=Confidence.HIGH,
            evidence=(evidence_2,),
        ),
    )

    result = _merge_claims(current, incoming)

    assert len(result) == 1
    assert result[0].text == "X is true"
    assert result[0].confidence == Confidence.HIGH
    assert len(result[0].evidence) == 2
    assert result[0].evidence[0].content_hash == "a" * 64
    assert result[0].evidence[1].content_hash == "b" * 64


def test_merge_claims_same_text_same_evidence_different_confidence() -> None:
    """单元测试: 同 text + 同 evidence + 不同 confidence → 1 条 claim, confidence = max。"""
    evidence_1 = _make_evidence(document_id="doc-1", content_hash="a" * 64)

    current = (
        KnowledgeClaim(
            text="X is true",
            confidence=Confidence.LOW,
            evidence=(evidence_1,),
        ),
    )
    incoming = (
        KnowledgeClaim(
            text="X is true",
            confidence=Confidence.HIGH,
            evidence=(evidence_1,),
        ),
    )

    result = _merge_claims(current, incoming)

    assert len(result) == 1
    assert result[0].text == "X is true"
    assert result[0].confidence == Confidence.HIGH  # max of LOW and HIGH is HIGH
    assert len(result[0].evidence) == 1
    assert result[0].evidence[0].content_hash == "a" * 64


def test_merge_claims_same_text_completely_unrelated_evidence() -> None:
    """单元测试: 同 text + 完全无关 evidence → 1 条 claim, evidence 合并。"""
    evidence_1 = _make_evidence(document_id="doc-1", content_hash="a" * 64)
    evidence_2 = _make_evidence(document_id="doc-2", content_hash="b" * 64)

    current = (
        KnowledgeClaim(
            text="X is true",
            confidence=Confidence.MEDIUM,
            evidence=(evidence_1,),
        ),
    )
    incoming = (
        KnowledgeClaim(
            text="X is true",
            confidence=Confidence.LOW,
            evidence=(evidence_2,),
        ),
    )

    result = _merge_claims(current, incoming)

    assert len(result) == 1
    assert result[0].text == "X is true"
    assert result[0].confidence == Confidence.MEDIUM  # max of MEDIUM and LOW is MEDIUM
    assert len(result[0].evidence) == 2
    assert {e.content_hash for e in result[0].evidence} == {"a" * 64, "b" * 64}


def test_merge_claims_different_text() -> None:
    """单元测试: 不同 text → 2 条 claim。"""
    evidence_1 = _make_evidence(document_id="doc-1", content_hash="a" * 64)
    evidence_2 = _make_evidence(document_id="doc-2", content_hash="b" * 64)

    current = (
        KnowledgeClaim(
            text="X is true",
            confidence=Confidence.HIGH,
            evidence=(evidence_1,),
        ),
    )
    incoming = (
        KnowledgeClaim(
            text="Y is true",
            confidence=Confidence.HIGH,
            evidence=(evidence_2,),
        ),
    )

    result = _merge_claims(current, incoming)

    assert len(result) == 2
    texts = {claim.text for claim in result}
    assert texts == {"X is true", "Y is true"}
