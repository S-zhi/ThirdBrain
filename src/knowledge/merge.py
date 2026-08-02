"""保守的 Artifact 合并规则。

LLM 可以给出建议，但只有 namespace、version、type、canonical_name 全部精确相同
时，服务才允许自动更新既有 Artifact。其它任何不确定情况都留给 review。
"""

from __future__ import annotations

from dataclasses import dataclass

from src.knowledge.models import (
    ActiveArtifact,
    ArtifactDraft,
    ArtifactStatus,
    ChangeAction,
    Confidence,
    KnowledgeClaim,
    MergeAction,
)


@dataclass(frozen=True, slots=True)
class MergeResolution:
    """一个 Draft 的确定性发布决策。"""

    action: ChangeAction
    status: ArtifactStatus
    artifact_id: str
    revision_number: int
    draft: ArtifactDraft
    source_ids: tuple[str, ...]


def _ordered_unique(values: tuple[str, ...], extra: tuple[str, ...]) -> tuple[str, ...]:
    """按首次出现顺序合并字符串，方便稳定 diff 和稳定索引。"""

    return tuple(dict.fromkeys((*values, *extra)))


def _claim_key(claim: KnowledgeClaim) -> str:
    """按 text 作为 Claim 的去重与合并键。"""
    return claim.text


def _merge_claims(
    current: tuple[KnowledgeClaim, ...],
    incoming: tuple[KnowledgeClaim, ...],
) -> tuple[KnowledgeClaim, ...]:
    """合并同一规范的 Claim：同 text 视为同一 fact，合并 evidence，confidence 取 max。"""
    by_key: dict[str, KnowledgeClaim] = {}

    def upsert(claim: KnowledgeClaim) -> None:
        key = _claim_key(claim)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = claim.model_copy(deep=True)
        else:
            # 合并 evidence (按 content_hash 去重)
            seen_hash = {e.content_hash for e in existing.evidence}
            merged_evidence = tuple(
                list(existing.evidence)
                + [e for e in claim.evidence if e.content_hash not in seen_hash]
            )
            # confidence 取 max
            levels = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}
            new_conf = max((existing.confidence, claim.confidence), key=levels.__getitem__)
            by_key[key] = existing.model_copy(
                update={
                    "evidence": merged_evidence,
                    "confidence": new_conf,
                }
            )

    for claim in (*current, *incoming):
        upsert(claim)
    return tuple(by_key.values())


def _merge_draft(current: ArtifactDraft, incoming: ArtifactDraft) -> ArtifactDraft:
    """只在身份完全一致时合并正文；新 Source 可以补充但不抹掉旧 Claim。"""

    current_relations = {
        (
            relation.relation_type,
            relation.target_wiki_id,
            relation.target_namespace,
            relation.target_version,
            relation.target_canonical_name,
        )
        for relation in current.related_artifacts
    }
    relations = list(current.related_artifacts)
    for relation in incoming.related_artifacts:
        key = (
            relation.relation_type,
            relation.target_wiki_id,
            relation.target_namespace,
            relation.target_version,
            relation.target_canonical_name,
        )
        if key not in current_relations:
            current_relations.add(key)
            relations.append(relation)
    return incoming.model_copy(
        update={
            "aliases": _ordered_unique(current.aliases, incoming.aliases),
            "claims": _merge_claims(current.claims, incoming.claims),
            "open_questions": _ordered_unique(current.open_questions, incoming.open_questions),
            "related_artifacts": tuple(relations),
        }
    )


class ConservativeMergePlanner:
    """将 Draft 对齐到既有 Artifact；拒绝模糊的跨实体合并。"""

    def resolve(
        self,
        draft: ArtifactDraft,
        candidates: tuple[ActiveArtifact, ...],
        *,
        source_id: str,
    ) -> MergeResolution:
        """为一个已验证 Draft 选择 create/update/review 结果。"""

        exact = tuple(
            candidate for candidate in candidates if candidate.artifact_id == draft.artifact_id
        )
        recommendation = draft.merge_recommendation
        if recommendation.action == MergeAction.NEEDS_REVIEW:
            return MergeResolution(
                action=ChangeAction.NEEDS_REVIEW,
                status=ArtifactStatus.PENDING_REVIEW,
                artifact_id=draft.artifact_id,
                revision_number=1,
                draft=draft,
                source_ids=(source_id,),
            )
        if len(exact) > 1:
            # 数据层不变量被破坏时不能“挑一个看起来对的”。
            return MergeResolution(
                action=ChangeAction.NEEDS_REVIEW,
                status=ArtifactStatus.PENDING_REVIEW,
                artifact_id=draft.artifact_id,
                revision_number=1,
                draft=draft,
                source_ids=(source_id,),
            )
        if not exact:
            return MergeResolution(
                action=ChangeAction.CREATED,
                status=ArtifactStatus.ACTIVE,
                artifact_id=draft.artifact_id,
                revision_number=1,
                draft=draft,
                source_ids=(source_id,),
            )

        current = exact[0]
        target_id = recommendation.target_artifact_id
        if recommendation.action == MergeAction.KEEP_SEPARATE or (
            target_id is not None and target_id != current.artifact_id
        ):
            return MergeResolution(
                action=ChangeAction.NEEDS_REVIEW,
                status=ArtifactStatus.PENDING_REVIEW,
                artifact_id=draft.artifact_id,
                revision_number=current.revision_number + 1,
                draft=draft,
                source_ids=(source_id,),
            )
        return MergeResolution(
            action=ChangeAction.UPDATED,
            status=ArtifactStatus.ACTIVE,
            artifact_id=current.artifact_id,
            revision_number=current.revision_number + 1,
            draft=_merge_draft(current.draft, draft),
            source_ids=_ordered_unique(
                current.source_ids,
                (source_id,),
            ),
        )
