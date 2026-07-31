"""来源无关同步服务的端到端临时目录测试。"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict

from src.doc_sync.adapters.base import AdapterContext, SourceAdapter
from src.doc_sync.adapters.factory import AdapterFactory
from src.doc_sync.config import DocumentSyncConfig
from src.doc_sync.models import (
    DocumentRef,
    FetchResult,
    LifecycleStatus,
    ParsedDocument,
    RunManifest,
    SyncOperation,
)
from src.doc_sync.service import DocumentSyncService


class StaticOptions(BaseModel):
    """定义静态测试来源的内容。"""

    model_config = ConfigDict(extra="forbid")

    title: str
    body: str
    uri: str = "memory://doc-1"
    status_code: int = 200
    relative_path: str = "doc.md"


class StaticAdapter(SourceAdapter):
    """返回固定内容以验证核心同步决策。"""

    adapter_type = "unit-static-service"
    config_model = StaticOptions

    def bootstrap(self, target_directory: Path) -> list[DocumentRef]:
        """静态测试来源不从目标目录发现引用。"""
        return []

    async def initial_refs(self) -> list[DocumentRef]:
        """返回唯一静态文档引用。"""
        return [
            DocumentRef(
                source_id=self.source_id,
                document_id="doc-1",
                canonical_uri=self.options.uri,
            )
        ]

    async def fetch(
        self,
        ref: DocumentRef,
        context: AdapterContext,
    ) -> FetchResult:
        """返回无需网络的内存响应。"""
        return FetchResult(
            requested_uri=ref.canonical_uri,
            final_uri=ref.canonical_uri,
            status_code=self.options.status_code,
            content_type="text/plain",
            body=self.options.body.encode(),
            fetched_at=datetime.now(UTC),
            response_hash="response-hash",
        )

    def parse(self, ref: DocumentRef, result: FetchResult) -> ParsedDocument:
        """把固定内容转换为通用 ParsedDocument。"""
        if result.status_code != 200:
            raise ValueError(f"transient HTTP {result.status_code}")
        artifact = f"# {self.options.title}\n\n{self.options.body}\n"
        return ParsedDocument(
            source_id=ref.source_id,
            document_id=ref.document_id,
            canonical_uri=ref.canonical_uri,
            title=self.options.title,
            normalized_content=artifact,
            artifact_content=artifact,
        )

    def discover_refs(self, document: ParsedDocument) -> list[DocumentRef]:
        """静态测试来源不发现其他文档。"""
        return []

    def propose_relative_path(
        self,
        document: ParsedDocument,
    ) -> PurePosixPath:
        """返回固定目标路径。"""
        return PurePosixPath(self.options.relative_path)


def _ensure_static_registered() -> None:
    """幂等注册静态测试 Adapter。"""
    if StaticAdapter.adapter_type not in AdapterFactory.available_types():
        AdapterFactory.register(StaticAdapter)


def _config(
    tmp_path: Path,
    *,
    body: str = "stable body",
    uri: str = "memory://doc-1",
    status_code: int = 200,
    relative_path: str = "doc.md",
) -> DocumentSyncConfig:
    """构造完全位于临时目录的同步配置。"""
    return DocumentSyncConfig.model_validate(
        {
            "workspace_root": tmp_path,
            "runtime": {"root_directory": "./runtime"},
            "http_defaults": {"respect_robots_txt": False},
            "sources": [
                {
                    "id": "static-source",
                    "target_directory": "./documents",
                    "adapter": {
                        "type": StaticAdapter.adapter_type,
                        "options": {
                            "title": "Static",
                            "body": body,
                            "uri": uri,
                            "status_code": status_code,
                            "relative_path": relative_path,
                        },
                    },
                }
            ],
        }
    )


def test_service_is_idempotent_and_restores_local_changes(tmp_path: Path) -> None:
    """首次写入、无变化跳过和本地漂移恢复应形成完整闭环。"""
    _ensure_static_registered()
    service = DocumentSyncService(_config(tmp_path))

    first, _ = asyncio.run(service.run("bootstrap", apply=True))
    target = tmp_path / "documents" / "doc.md"
    assert target.read_text(encoding="utf-8") == "# Static\n\nstable body\n"
    assert first.documents[0].operation == SyncOperation.ADDED
    assert first.updated_markdown == ["documents/doc.md"]

    original_mtime = target.stat().st_mtime_ns
    second, _ = asyncio.run(service.run("sync", apply=True))
    assert second.documents[0].operation == SyncOperation.UNCHANGED
    assert second.updated_markdown == []
    assert target.stat().st_mtime_ns == original_mtime

    target.write_text("manual edit\n", encoding="utf-8")
    restored, _ = asyncio.run(service.run("sync", apply=True))
    assert restored.documents[0].operation == SyncOperation.RESTORED
    assert target.read_text(encoding="utf-8") == "# Static\n\nstable body\n"


def test_dry_run_never_writes_target_or_state(tmp_path: Path) -> None:
    """dry-run 只能生成 staging 和 manifest，不能写目标或持久化 state。"""
    _ensure_static_registered()
    service = DocumentSyncService(_config(tmp_path))
    manifest, manifest_path = asyncio.run(service.run("bootstrap", apply=False))
    assert manifest.documents[0].operation == SyncOperation.ADDED
    assert not (tmp_path / "documents" / "doc.md").exists()
    assert not (tmp_path / "runtime" / "state" / "static-source.json").exists()
    assert manifest_path.is_file()
    persisted = RunManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    assert persisted == manifest


def test_source_uri_move_keeps_identity_and_updates_state(tmp_path: Path) -> None:
    """同一 document_id 的新入口 URI 应记为 moved 而非新增文档。"""
    _ensure_static_registered()
    first_service = DocumentSyncService(_config(tmp_path, uri="memory://old/doc-1"))
    asyncio.run(first_service.run("bootstrap", apply=True))

    moved_service = DocumentSyncService(_config(tmp_path, uri="memory://new/doc-1"))
    manifest, _ = asyncio.run(moved_service.run("sync", apply=True))
    state = moved_service.storage.load_state("static-source")

    assert manifest.documents[0].operation == SyncOperation.MOVED
    assert manifest.updated_markdown == ["documents/doc.md"]
    assert state is not None
    assert state.entries[0].canonical_uri == "memory://new/doc-1"


def test_missing_count_reaches_archived_candidate_without_deleting(
    tmp_path: Path,
) -> None:
    """连续三次明确 404 应只标记候选归档并保留 Markdown。"""
    _ensure_static_registered()
    initial = DocumentSyncService(_config(tmp_path))
    asyncio.run(initial.run("bootstrap", apply=True))
    target = tmp_path / "documents" / "doc.md"

    operations: list[SyncOperation] = []
    service: DocumentSyncService | None = None
    for _ in range(3):
        service = DocumentSyncService(_config(tmp_path, status_code=404))
        manifest, _ = asyncio.run(service.run("sync", apply=True))
        operations.append(manifest.documents[0].operation)

    assert operations == [
        SyncOperation.MISSING,
        SyncOperation.MISSING,
        SyncOperation.ARCHIVED_CANDIDATE,
    ]
    assert target.is_file()
    assert service is not None
    state = service.storage.load_state("static-source")
    assert state is not None
    assert state.entries[0].missing_count == 3
    assert state.entries[0].lifecycle_status == LifecycleStatus.ARCHIVED_CANDIDATE


def test_transient_failure_does_not_increase_missing_count(tmp_path: Path) -> None:
    """5xx 解析失败应保留上次明确缺失计数，不构成删除证据。"""
    _ensure_static_registered()
    initial = DocumentSyncService(_config(tmp_path))
    asyncio.run(initial.run("bootstrap", apply=True))
    missing = DocumentSyncService(_config(tmp_path, status_code=404))
    asyncio.run(missing.run("sync", apply=True))

    transient = DocumentSyncService(_config(tmp_path, status_code=503))
    manifest, _ = asyncio.run(transient.run("sync", apply=True))
    state = transient.storage.load_state("static-source")

    assert manifest.documents[0].operation == SyncOperation.FAILED
    assert state is not None
    assert state.entries[0].missing_count == 1


def test_unregistered_file_path_collision_is_preserved(tmp_path: Path) -> None:
    """新文档不得覆盖占用建议路径但未登记的本地文件。"""
    _ensure_static_registered()
    original = tmp_path / "documents" / "doc.md"
    original.parent.mkdir()
    original.write_text("local-only\n", encoding="utf-8")

    service = DocumentSyncService(_config(tmp_path))
    manifest, _ = asyncio.run(service.run("bootstrap", apply=True))
    generated = list((tmp_path / "documents").glob("doc--doc-1-*.md"))

    assert original.read_text(encoding="utf-8") == "local-only\n"
    assert len(generated) == 1
    assert manifest.updated_markdown == [generated[0].relative_to(tmp_path).as_posix()]


def test_adapter_path_traversal_is_rejected(tmp_path: Path) -> None:
    """Adapter 返回含 .. 的路径时必须失败且不能写出目标目录。"""
    _ensure_static_registered()
    service = DocumentSyncService(_config(tmp_path, relative_path="../outside.md"))

    manifest, _ = asyncio.run(service.run("bootstrap", apply=True))

    assert manifest.documents[0].operation == SyncOperation.FAILED
    assert manifest.status == "partial"
    assert not (tmp_path / "outside.md").exists()


def test_updated_file_creates_backup_and_resume_reconciles_state(
    tmp_path: Path,
) -> None:
    """覆盖前应备份，resume 应补写已完成 action 尚未落盘的 state。"""
    _ensure_static_registered()
    initial = DocumentSyncService(_config(tmp_path))
    asyncio.run(initial.run("bootstrap", apply=True))

    updated = DocumentSyncService(_config(tmp_path, body="changed body"))
    manifest, _ = asyncio.run(updated.run("sync", apply=True))
    backup = (
        tmp_path
        / "runtime"
        / "backups"
        / manifest.run_id
        / "static-source"
        / "documents"
        / "doc.md"
    )
    assert backup.read_text(encoding="utf-8") == "# Static\n\nstable body\n"

    journal = updated.storage.load_journal(manifest.run_id)
    journal.actions[0].completed = False
    updated.storage.save_journal(journal)
    state_path = tmp_path / "runtime" / "state" / "static-source.json"
    state_path.unlink()
    asyncio.run(updated.resume(manifest.run_id))
    state = updated.storage.load_state("static-source")
    assert state is not None
    assert state.entries[0].content_hash == manifest.documents[0].new_content_hash
    assert backup.read_text(encoding="utf-8") == "# Static\n\nstable body\n"

    state_path.unlink()
    asyncio.run(updated.resume(manifest.run_id))
    assert updated.storage.load_state("static-source") is not None


def test_multiple_sources_keep_targets_and_states_isolated(tmp_path: Path) -> None:
    """多个 source 应分别写入目标目录和独立 JSON state。"""
    _ensure_static_registered()
    config = DocumentSyncConfig.model_validate(
        {
            "workspace_root": tmp_path,
            "runtime": {"root_directory": "./runtime"},
            "http_defaults": {"respect_robots_txt": False},
            "sources": [
                {
                    "id": "source-a",
                    "target_directory": "./documents-a",
                    "adapter": {
                        "type": StaticAdapter.adapter_type,
                        "options": {"title": "A", "body": "body-a"},
                    },
                },
                {
                    "id": "source-b",
                    "target_directory": "./documents-b",
                    "adapter": {
                        "type": StaticAdapter.adapter_type,
                        "options": {"title": "B", "body": "body-b"},
                    },
                },
            ],
        }
    )

    service = DocumentSyncService(config)
    manifest, _ = asyncio.run(service.run("bootstrap", apply=True))

    assert manifest.stats.sources == 2
    assert (tmp_path / "documents-a" / "doc.md").is_file()
    assert (tmp_path / "documents-b" / "doc.md").is_file()
    assert (tmp_path / "runtime" / "state" / "source-a.json").is_file()
    assert (tmp_path / "runtime" / "state" / "source-b.json").is_file()
