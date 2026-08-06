"""对 LLM 派生知识执行确定性来源与范围校验。"""

from __future__ import annotations

from src.knowledge.models import (
    ArtifactDraft,
    ExtractionResult,
    KnowledgeDocumentInput,
    ValidationIssue,
    ValidationSummary,
)


def _issue(
    code: str,
    message: str,
    document: KnowledgeDocumentInput,
    artifact: ArtifactDraft | None = None,
) -> ValidationIssue:
    """构造包含最小定位信息的 validation issue。"""

    return ValidationIssue(
        code=code,
        message=message,
        document_id=document.document_id,
        artifact_id=artifact.artifact_id if artifact else None,
    )


def validate_extraction(
    document: KnowledgeDocumentInput,
    extraction: ExtractionResult,
) -> ValidationSummary:
    """验证提取结果只能表达当前原始文档已提供的可追溯事实。

    这里刻意不用 LLM 复核。任何没有来源、越出当前 namespace/version，或无法
    在原始 Part 中定位的 Claim 都阻断发布，避免把模型推测降级成“低置信事实”。
    """

    issues: list[ValidationIssue] = []
    parts = {part.part_id: part for part in document.parts}
    for artifact in extraction.artifacts:
        if (
            artifact.wiki_id != document.wiki_id
            or artifact.namespace != document.namespace
            or artifact.version != document.version
        ):
            issues.append(
                _issue(
                    "ARTIFACT_SCOPE_MISMATCH",
                    "Artifact 的 wiki_id、namespace/version 必须与当前 Source 完全一致",
                    document,
                    artifact,
                )
            )
        for claim in artifact.claims:
            for evidence in claim.evidence:
                if not evidence.namespace:
                    evidence.namespace = document.namespace
                if not evidence.version:
                    evidence.version = document.version

                if evidence.namespace != document.namespace:
                    issues.append(
                        _issue(
                            "EVIDENCE_NAMESPACE_MISMATCH",
                            f"Claim evidence 引用了 namespace={evidence.namespace!r} 的 document，"
                            f"与当前 Source namespace={document.namespace!r} 不一致",
                            document,
                            artifact,
                        )
                    )
                if evidence.version != document.version:
                    issues.append(
                        _issue(
                            "EVIDENCE_VERSION_MISMATCH",
                            f"Claim evidence 引用了 version={evidence.version!r} 的 document，"
                            f"与当前 Source version={document.version!r} 不一致",
                            document,
                            artifact,
                        )
                    )

                if (
                    evidence.document_id != document.document_id
                    or evidence.rag_collection_id != document.rag_collection_id
                ):
                    issues.append(
                        _issue(
                            "EVIDENCE_DOCUMENT_MISMATCH",
                            "Claim evidence 引用了当前 RAG Collection 以外的 document",
                            document,
                            artifact,
                        )
                    )
                    continue
                part = parts.get(evidence.part_id)
                if part is None:
                    issues.append(
                        _issue(
                            "EVIDENCE_PART_NOT_FOUND",
                            f"Claim evidence 引用了不存在的 part_id: {evidence.part_id}",
                            document,
                            artifact,
                        )
                    )
                    continue
                if evidence.content_hash != part.content_hash:
                    issues.append(
                        _issue(
                            "EVIDENCE_CONTENT_HASH_MISMATCH",
                            "Claim evidence 的 content_hash 不匹配当前原始 Part",
                            document,
                            artifact,
                        )
                    )
                if evidence.quote_hint not in part.content:
                    issues.append(
                        _issue(
                            "EVIDENCE_QUOTE_NOT_FOUND",
                            "quote_hint 无法在声明的原始 Part 中定位",
                            document,
                            artifact,
                        )
                    )
                if evidence.char_end is not None and evidence.char_end > len(part.content):
                    issues.append(
                        _issue(
                            "EVIDENCE_RANGE_OUT_OF_BOUNDS",
                            "Evidence 字符范围超出原始 Part 内容",
                            document,
                            artifact,
                        )
                    )
                if (
                    evidence.char_start is not None
                    and evidence.char_end is not None
                    and evidence.quote_hint
                    not in part.content[evidence.char_start : evidence.char_end]
                ):
                    issues.append(
                        _issue(
                            "EVIDENCE_RANGE_QUOTE_MISMATCH",
                            "Evidence 字符范围没有覆盖 quote_hint",
                            document,
                            artifact,
                        )
                    )
        for relation in artifact.related_artifacts:
            if (
                relation.target_wiki_id != document.wiki_id
                or relation.target_namespace != document.namespace
                or relation.target_version != document.version
            ):
                issues.append(
                    _issue(
                        "RELATION_SCOPE_MISMATCH",
                        "跨 Wiki/namespace/version 关系不能进入 Knowledge 图",
                        document,
                        artifact,
                    )
                )
            for evidence in relation.evidence:
                if not evidence.namespace:
                    evidence.namespace = document.namespace
                if not evidence.version:
                    evidence.version = document.version

                if evidence.namespace != document.namespace:
                    issues.append(
                        _issue(
                            "RELATION_EVIDENCE_NAMESPACE_MISMATCH",
                            f"关系 Evidence 引用了 namespace={evidence.namespace!r} 的 document，"
                            f"与当前 Source namespace={document.namespace!r} 不一致",
                            document,
                            artifact,
                        )
                    )
                if evidence.version != document.version:
                    issues.append(
                        _issue(
                            "RELATION_EVIDENCE_VERSION_MISMATCH",
                            f"关系 Evidence 引用了 version={evidence.version!r} 的 document，"
                            f"与当前 Source version={document.version!r} 不一致",
                            document,
                            artifact,
                        )
                    )

                part = parts.get(evidence.part_id)
                if (
                    evidence.document_id != document.document_id
                    or evidence.rag_collection_id != document.rag_collection_id
                    or part is None
                ):
                    issues.append(
                        _issue(
                            "RELATION_EVIDENCE_NOT_FOUND",
                            "关系证据必须引用当前 Source 内存在的 Part",
                            document,
                            artifact,
                        )
                    )
                    continue
                if (
                    evidence.content_hash != part.content_hash
                    or evidence.quote_hint not in part.content
                ):
                    issues.append(
                        _issue(
                            "RELATION_EVIDENCE_INVALID",
                            "关系证据无法和当前原始 Part 对齐",
                            document,
                            artifact,
                        )
                    )
                if (
                    evidence.char_start is not None
                    and evidence.char_end is not None
                    and evidence.quote_hint
                    not in part.content[evidence.char_start : evidence.char_end]
                ):
                    issues.append(
                        _issue(
                            "RELATION_EVIDENCE_RANGE_QUOTE_MISMATCH",
                            "关系 Evidence 字符范围没有覆盖 quote_hint",
                            document,
                            artifact,
                        )
                    )
    return ValidationSummary(passed=not issues, issues=tuple(issues))
