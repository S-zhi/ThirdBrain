"""tests/unit/test_mongo_repository.py — 单测 MongoKnowledgeRepository。

使用 Mock 对象模拟 PyMongo，验证 multi-document 事务支持、
DuplicateKeyError 幂等容错、发布重试、以及 abandon 状态机自愈。
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, UTC
from pymongo.errors import DuplicateKeyError, PyMongoError

from src.knowledge.mongo_repository import MongoKnowledgeRepository, active_catalog_id
from src.knowledge.models import (
    SourceRevision,
    ArtifactRevision,
    ArtifactStatus,
    ArtifactType,
    ArtifactDraft,
    KnowledgeDocumentInput,
    SourcePart,
    stable_source_id,
    Confidence,
    EvidenceRef,
    KnowledgeClaim,
)


def _mock_document() -> KnowledgeDocumentInput:
    return KnowledgeDocumentInput(
        document_id="doc-1",
        wiki_id="wiki-1",
        rag_collection_id="rag-1",
        namespace="namespace-1",
        version="v1",
        content_hash="a" * 64,
        parts=(
            SourcePart(
                part_id="part-1",
                order=1,
                content="some content",
            ),
        ),
    )


def _mock_source_revision() -> SourceRevision:
    doc = _mock_document()
    return SourceRevision(
        source_revision_id="sr_test_123",
        source_id=doc.source_id,
        wiki_id=doc.wiki_id,
        document=doc,
        revision_number=1,
        compiler_fingerprint="f" * 32,
        created_at=datetime.now(UTC),
    )


def _mock_artifact_revision() -> ArtifactRevision:
    doc = _mock_document()
    draft = ArtifactDraft(
        artifact_type=ArtifactType.CONCEPT,
        wiki_id=doc.wiki_id,
        namespace=doc.namespace,
        version=doc.version,
        canonical_name="concept-1",
        title="concept-1",
        summary="concept-1 summary",
        claims=(
            KnowledgeClaim(
                text="some claim",
                confidence=Confidence.HIGH,
                evidence=(
                    EvidenceRef(
                        document_id="doc-1",
                        rag_collection_id="rag-1",
                        part_id="part-1",
                        content_hash="a" * 64,
                        quote_hint="some content",
                    ),
                ),
            ),
        ),
    )
    return ArtifactRevision(
        artifact_revision_id="ar_test_456",
        artifact_id=draft.artifact_id,
        wiki_id=doc.wiki_id,
        source_revision_id="sr_test_123",
        revision_number=1,
        status=ArtifactStatus.ACTIVE,
        draft=draft,
        source_ids=(doc.source_id,),
        extractor_version="v1",
        prompt_version="v1",
        model="model-1",
        schema_version="1",
        created_at=datetime.now(UTC),
    )


class TestMongoKnowledgeRepositoryPublish:
    @pytest.mark.asyncio
    async def test_publish_already_published_returns_active(self) -> None:
        """如果 Staging state 已经是 "published"，直接幂等返回 active 记录。"""
        # Arrange
        mongo_mock = MagicMock()
        repo = MongoKnowledgeRepository(mongo_mock)

        source = _mock_source_revision()
        artifact = _mock_artifact_revision()

        staging_entry = {
            "_id": "stg_123",
            "state": "published",
            "source_revision": source.model_dump(mode="python"),
            "artifact_revisions": [artifact.model_dump(mode="python")],
        }

        # Mock collection find_one
        repo._staging = MagicMock()
        repo._staging.return_value.find_one = AsyncMock(return_value=staging_entry)

        # Act
        result = await repo.publish("stg_123")

        # Assert
        assert len(result) == 1
        assert result[0].artifact_revision_id == artifact.artifact_revision_id

    @pytest.mark.asyncio
    async def test_publish_abandoned_raises_error(self) -> None:
        """如果 Staging state 已经是 "abandoned"，抛出 RuntimeError。"""
        # Arrange
        mongo_mock = MagicMock()
        repo = MongoKnowledgeRepository(mongo_mock)

        staging_entry = {
            "_id": "stg_123",
            "state": "abandoned",
        }

        repo._staging = MagicMock()
        repo._staging.return_value.find_one = AsyncMock(return_value=staging_entry)

        # Act & Assert
        with pytest.raises(RuntimeError, match="knowledge staging is not publishable: abandoned"):
            await repo.publish("stg_123")

    @pytest.mark.asyncio
    async def test_publish_non_transaction_mode_success(self) -> None:
        """Standalone 模式下（不启用事务）正常发布成功。"""
        # Arrange
        mongo_mock = MagicMock()
        mongo_mock.settings.use_transactions = False
        repo = MongoKnowledgeRepository(mongo_mock)

        source = _mock_source_revision()
        artifact = _mock_artifact_revision()

        staging_entry = {
            "_id": "stg_123",
            "wiki_id": source.wiki_id,
            "catalog_id": "wiki_catalog_123",
            "catalog_revision": 1,
            "state": "staged",
            "source_revision": source.model_dump(mode="python"),
            "artifact_revisions": [artifact.model_dump(mode="python")],
        }

        # Mock collections
        repo._staging = MagicMock()
        repo._staging.return_value.find_one = AsyncMock(return_value=staging_entry)
        repo._staging.return_value.update_one = AsyncMock()

        repo._sources = MagicMock()
        repo._sources.return_value.insert_one = AsyncMock()

        repo._artifacts = MagicMock()
        repo._artifacts.return_value.insert_one = AsyncMock()

        repo._catalog = MagicMock()
        repo._catalog.return_value.find_one_and_update = AsyncMock(return_value={"revision": 2})

        # Act
        result = await repo.publish("stg_123")

        # Assert
        assert len(result) == 1
        assert result[0].artifact_revision_id == artifact.artifact_revision_id

        # Verify calls
        repo._sources.return_value.insert_one.assert_called_once()
        repo._artifacts.return_value.insert_one.assert_called_once()
        repo._catalog.return_value.find_one_and_update.assert_called_once()
        repo._staging.return_value.update_one.assert_called_once()

    @pytest.mark.asyncio
    async def test_publish_transaction_mode_success(self) -> None:
        """多文档事务模式下，启动 session 并开启事务完成发布。"""
        # Arrange
        mongo_mock = MagicMock()
        mongo_mock.settings.use_transactions = True

        session_mock = AsyncMock()
        mongo_mock.client.start_session = AsyncMock(return_value=session_mock)
        # Use MagicMock context manager for transaction
        session_mock.__aenter__ = AsyncMock(return_value=session_mock)
        session_mock.__aexit__ = AsyncMock()
        session_mock.start_transaction = MagicMock()

        repo = MongoKnowledgeRepository(mongo_mock)

        source = _mock_source_revision()
        artifact = _mock_artifact_revision()

        staging_entry = {
            "_id": "stg_123",
            "wiki_id": source.wiki_id,
            "catalog_id": "wiki_catalog_123",
            "catalog_revision": 1,
            "state": "staged",
            "source_revision": source.model_dump(mode="python"),
            "artifact_revisions": [artifact.model_dump(mode="python")],
        }

        # Mock collections
        repo._staging = MagicMock()
        repo._staging.return_value.find_one = AsyncMock(return_value=staging_entry)
        repo._staging.return_value.update_one = AsyncMock()

        repo._sources = MagicMock()
        repo._sources.return_value.insert_one = AsyncMock()

        repo._artifacts = MagicMock()
        repo._artifacts.return_value.insert_one = AsyncMock()

        repo._catalog = MagicMock()
        repo._catalog.return_value.find_one_and_update = AsyncMock(return_value={"revision": 2})

        # Act
        result = await repo.publish("stg_123")

        # Assert
        assert len(result) == 1
        assert result[0].artifact_revision_id == artifact.artifact_revision_id

        # Verify session is passed and transaction is started
        mongo_mock.client.start_session.assert_called_once()
        repo._sources.return_value.insert_one.assert_called_with(
            {"_id": "sr_test_123", **source.model_dump(mode="python")}, session=session_mock
        )
        repo._staging.return_value.update_one.assert_called_with(
            {"_id": "stg_123"},
            {"$set": {"state": "published", "reason": ""}},
            session=session_mock,
        )

    @pytest.mark.asyncio
    async def test_publish_step1_duplicate_key_error_ignored(self) -> None:
        """Step 1 遇到 DuplicateKeyError（即已经被并发写入或 retry）时应自动忽略，实现幂等。"""
        # Arrange
        mongo_mock = MagicMock()
        mongo_mock.settings.use_transactions = False
        repo = MongoKnowledgeRepository(mongo_mock)

        source = _mock_source_revision()
        artifact = _mock_artifact_revision()

        staging_entry = {
            "_id": "stg_123",
            "wiki_id": source.wiki_id,
            "catalog_id": "wiki_catalog_123",
            "catalog_revision": 1,
            "state": "staged",
            "source_revision": source.model_dump(mode="python"),
            "artifact_revisions": [artifact.model_dump(mode="python")],
        }

        repo._staging = MagicMock()
        repo._staging.return_value.find_one = AsyncMock(return_value=staging_entry)
        repo._staging.return_value.update_one = AsyncMock()

        # Step 1 raises DuplicateKeyError
        repo._sources = MagicMock()
        repo._sources.return_value.insert_one = AsyncMock(
            side_effect=DuplicateKeyError("Duplicate key")
        )

        repo._artifacts = MagicMock()
        repo._artifacts.return_value.insert_one = AsyncMock(
            side_effect=DuplicateKeyError("Duplicate key")
        )

        repo._catalog = MagicMock()
        repo._catalog.return_value.find_one_and_update = AsyncMock(return_value={"revision": 2})

        # Act
        result = await repo.publish("stg_123")

        # Assert
        assert len(result) == 1
        # Success since DuplicateKeyError was caught and ignored
        repo._catalog.return_value.find_one_and_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_publish_step2_retry_idempotency_pointers_match(self) -> None:
        """Step 2 find_one_and_update 冲突返回 None，但检查发现 catalog 确实已被此 staging 成功更新，继续完成 publish。"""
        # Arrange
        mongo_mock = MagicMock()
        mongo_mock.settings.use_transactions = False
        repo = MongoKnowledgeRepository(mongo_mock)

        source = _mock_source_revision()
        artifact = _mock_artifact_revision()

        staging_entry = {
            "_id": "stg_123",
            "wiki_id": source.wiki_id,
            "catalog_id": "wiki_catalog_123",
            "catalog_revision": 1,
            "state": "staged",
            "source_revision": source.model_dump(mode="python"),
            "artifact_revisions": [artifact.model_dump(mode="python")],
        }

        repo._staging = MagicMock()
        repo._staging.return_value.find_one = AsyncMock(return_value=staging_entry)
        repo._staging.return_value.update_one = AsyncMock()

        repo._sources = MagicMock()
        repo._sources.return_value.insert_one = AsyncMock()

        repo._artifacts = MagicMock()
        repo._artifacts.return_value.insert_one = AsyncMock()

        # Step 2 find_one_and_update returns None (optimistic lock fail on retry)
        repo._catalog = MagicMock()
        repo._catalog.return_value.find_one_and_update = AsyncMock(return_value=None)

        # But find_one (checking current catalog) reveals source_revision_id MATCHES ours!
        current_catalog = {
            "_id": "wiki_catalog_123",
            "revision": 2,
            "sources": {
                source.source_id: {
                    "source_revision_id": source.source_revision_id,
                }
            }
        }
        repo._catalog.return_value.find_one = AsyncMock(return_value=current_catalog)

        # Act
        result = await repo.publish("stg_123")

        # Assert
        assert len(result) == 1
        repo._staging.return_value.update_one.assert_called_with(
            {"_id": "stg_123"},
            {"$set": {"state": "published", "reason": ""}},
            session=None,
        )


class TestMongoKnowledgeRepositoryAbandon:
    @pytest.mark.asyncio
    async def test_abandon_published_raises_error(self) -> None:
        """已经是 published 的 Staging 不能被 abandon。"""
        # Arrange
        mongo_mock = MagicMock()
        repo = MongoKnowledgeRepository(mongo_mock)

        staging_entry = {
            "_id": "stg_123",
            "state": "published",
        }
        repo._staging = MagicMock()
        repo._staging.return_value.find_one = AsyncMock(return_value=staging_entry)

        # Act & Assert
        with pytest.raises(RuntimeError, match="published staging cannot be abandoned"):
            await repo.abandon("stg_123", "some reason")

    @pytest.mark.asyncio
    async def test_abandon_normal_success(self) -> None:
        """正常情况下（未发布成功且 catalog pointers 未改变）abandon 正确设置为 "abandoned" 状态。"""
        # Arrange
        mongo_mock = MagicMock()
        repo = MongoKnowledgeRepository(mongo_mock)

        source = _mock_source_revision()
        staging_entry = {
            "_id": "stg_123",
            "wiki_id": source.wiki_id,
            "state": "staged",
            "source_revision": source.model_dump(mode="python"),
        }
        repo._staging = MagicMock()
        repo._staging.return_value.find_one = AsyncMock(return_value=staging_entry)
        repo._staging.return_value.update_one = AsyncMock()

        repo._catalog = MagicMock()
        # Catalog is empty/doesn't match our source_revision_id
        repo._catalog.return_value.find_one = AsyncMock(return_value=None)

        # Act
        await repo.abandon("stg_123", "some failure")

        # Assert
        repo._staging.return_value.update_one.assert_called_with(
            {"_id": "stg_123"},
            {"$set": {"state": "abandoned", "reason": "some failure"}},
            session=None,
        )

    @pytest.mark.asyncio
    async def test_abandon_self_heals_to_published(self) -> None:
        """当 abandon 被调用，但检测到 catalog 的指针已经指向我们的 source_revision_id，说明 Step 2 实际已经成功发布，自愈标记 state="published"。"""
        # Arrange
        mongo_mock = MagicMock()
        repo = MongoKnowledgeRepository(mongo_mock)

        source = _mock_source_revision()
        staging_entry = {
            "_id": "stg_123",
            "wiki_id": source.wiki_id,
            "state": "staged",
            "source_revision": source.model_dump(mode="python"),
        }
        repo._staging = MagicMock()
        repo._staging.return_value.find_one = AsyncMock(return_value=staging_entry)
        repo._staging.return_value.update_one = AsyncMock()

        # Catalog already has our source_revision_id
        current_catalog = {
            "_id": active_catalog_id(source.wiki_id),
            "sources": {
                source.source_id: {
                    "source_revision_id": source.source_revision_id,
                }
            }
        }
        repo._catalog = MagicMock()
        repo._catalog.return_value.find_one = AsyncMock(return_value=current_catalog)

        # Act
        await repo.abandon("stg_123", "some failure")

        # Assert
        # Instead of "abandoned", it self-heals to "published"!
        repo._staging.return_value.update_one.assert_called_with(
            {"_id": "stg_123"},
            {"$set": {"state": "published", "reason": ""}},
            session=None,
        )
