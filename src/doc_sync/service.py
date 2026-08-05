"""来源无关的发现、Hash、Diff、写入、状态和 manifest 编排服务。"""

from __future__ import annotations

import asyncio
import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from src.doc_sync.adapters import AdapterContext, AdapterFactory, SourceAdapter
from src.doc_sync.config import DocumentSyncConfig, SourceConfig
from src.doc_sync.errors import DocumentSyncError, PathSafetyError
from src.doc_sync.http import HttpFetchClient
from src.doc_sync.models import (
    ApplyJournal,
    DocumentManifestEntry,
    DocumentRef,
    JournalAction,
    LifecycleStatus,
    ParsedDocument,
    RunManifest,
    RunStats,
    SourceRunResult,
    SourceState,
    SyncOperation,
    SyncStateEntry,
)
from src.doc_sync.storage import SyncStorage, resolve_under, safe_relative_path


def sha256_text(value: str) -> str:
    """计算 UTF-8 文本的稳定 SHA-256。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str | None:
    """计算现有文件 SHA-256；文件不存在时返回 None。"""
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _semantic_text(value: str) -> str:
    """把新旧 Markdown 压缩成可用于首次基线对齐的可见文本序列。"""
    text = unicodedata.normalize("NFC", value)
    text = re.sub(r"^>\s*(来源|节点)\s*[:：].*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[#>*_`|~\-\[\](){}]", "", text)
    return re.sub(r"\s+", "", text)


def _legacy_equivalent(remote_content: str, local_content: str) -> bool:
    """判断规范正文是否完整存在于带旧站点导航噪声的本地 Markdown。"""
    remote = _semantic_text(remote_content)
    local = _semantic_text(local_content)
    return len(remote) >= 20 and remote in local


def _safe_document_id_fragment(document_id: str) -> str:
    """生成适合追加到文件名且抗碰撞的稳定文档 ID 片段。"""
    readable = re.sub(r"[^0-9A-Za-z._-]+", "_", document_id).strip("._-")
    readable = readable[:48] or "document"
    digest = hashlib.sha256(document_id.encode("utf-8")).hexdigest()[:10]
    return f"{readable}-{digest}"


@dataclass(slots=True)
class SourceExecution:
    """保存一个 source 执行后的 manifest、状态和待应用动作。"""

    source: SourceConfig
    adapter: SourceAdapter
    documents: list[DocumentManifestEntry]
    state_entries: dict[str, SyncStateEntry]
    actions: list[JournalAction]
    errors: list[str]
    identity_drifts: list[str]
    status: str


class DocumentSyncService:
    """执行多来源文档同步且不依赖任何具体 Adapter。"""

    def __init__(self, config: DocumentSyncConfig) -> None:
        """注入强校验配置并准备运行时存储。"""
        self.config = config
        AdapterFactory.validate_sources(config.sources)
        self.storage = SyncStorage(config)

    def _workspace_relative(self, path: Path) -> str:
        """把工作区内绝对路径转换为 POSIX 相对路径。"""
        resolved = path.resolve()
        if not resolved.is_relative_to(self.config.workspace_root):
            raise PathSafetyError(f"路径不属于 workspace_root: {resolved}")
        return resolved.relative_to(self.config.workspace_root).as_posix()

    def _target_for_relative(self, workspace_relative: str) -> Path:
        """把 manifest/state 中的相对路径解析为安全目标。"""
        return resolve_under(self.config.workspace_root, workspace_relative)

    def _state_entry_priority(
        self,
        source: SourceConfig,
        entry: SyncStateEntry,
        preferred_path: str | None,
    ) -> tuple[int, int, int, int, str]:
        """为状态迁移选择更可信的来源路径，优先现有层级和真实文件。"""
        preferred = int(preferred_path is not None and entry.relative_path == preferred_path)
        target = self._target_for_relative(entry.relative_path)
        exists = int(target.is_file())
        depth = len(Path(entry.relative_path).parts)
        try:
            mtime = target.stat().st_mtime_ns
        except OSError:
            mtime = 0
        return (preferred, exists, depth, -mtime, entry.relative_path)

    def _reconcile_state_entries(
        self,
        source: SourceConfig,
        entries: list[SyncStateEntry],
        bootstrap_refs: list[DocumentRef],
    ) -> dict[str, SyncStateEntry]:
        """按 canonical URI 合并旧状态别名并迁移到当前 Adapter 身份。"""
        refs_by_uri = {ref.canonical_uri: ref for ref in bootstrap_refs}
        reconciled: dict[str, SyncStateEntry] = {}
        for entry in entries:
            ref = refs_by_uri.get(entry.canonical_uri)
            document_id = ref.document_id if ref is not None else entry.document_id
            candidate = entry.model_copy(update={"document_id": document_id})
            current = reconciled.get(document_id)
            preferred_path = None
            if ref is not None and ref.relative_path_hint:
                source_target = self.config.source_target(source)
                preferred_path = self._workspace_relative(
                    resolve_under(source_target, ref.relative_path_hint)
                )
            if current is None or self._state_entry_priority(
                source,
                candidate,
                preferred_path,
            ) > self._state_entry_priority(source, current, preferred_path):
                reconciled[document_id] = candidate
        return reconciled

    def _choose_path(
        self,
        source: SourceConfig,
        adapter: SourceAdapter,
        document: ParsedDocument,
        ref: DocumentRef,
        existing: SyncStateEntry | None,
        claimed_paths: dict[str, str],
    ) -> tuple[str, bool]:
        """按状态、现有路径和 Adapter 建议依次选择安全目标路径。"""
        target_directory = self.config.source_target(source)
        needs_classification = bool(document.metadata.get("needs_classification", False))
        if ref.relative_path_hint:
            candidate = resolve_under(target_directory, ref.relative_path_hint)
            workspace_relative = self._workspace_relative(candidate)
        elif existing is not None:
            workspace_relative = existing.relative_path
        else:
            proposed = adapter.propose_relative_path(document)
            if proposed is None:
                proposed = Path("_unresolved") / f"{document.document_id}.md"
                needs_classification = True
            proposed_path = safe_relative_path(str(proposed))
            candidate = resolve_under(target_directory, proposed_path.as_posix())
            workspace_relative = self._workspace_relative(candidate)
        target = self._target_for_relative(workspace_relative)
        if not target.is_relative_to(target_directory):
            raise PathSafetyError(
                f"source {source.id!r} 的文档路径逃逸目标目录: {workspace_relative}"
            )
        claimed_by = claimed_paths.get(workspace_relative)
        is_registered_target = existing is not None or ref.relative_path_hint is not None
        has_collision = (claimed_by is not None and claimed_by != document.document_id) or (
            target.exists() and not is_registered_target and claimed_by is None
        )
        if has_collision:
            if self.config.policies.path_collision.strategy == "fail":
                raise PathSafetyError(
                    f"路径碰撞 {workspace_relative}: "
                    f"{claimed_by or '未登记现有文件'}, {document.document_id}"
                )
            base_path = Path(workspace_relative)
            suffix = _safe_document_id_fragment(document.document_id)
            sequence = 1
            while True:
                sequence_suffix = "" if sequence == 1 else f"-{sequence}"
                collision_path = (
                    base_path.parent
                    / f"{base_path.stem}--{suffix}{sequence_suffix}{base_path.suffix}"
                )
                workspace_relative = safe_relative_path(collision_path.as_posix()).as_posix()
                target = self._target_for_relative(workspace_relative)
                if workspace_relative not in claimed_paths and not target.exists():
                    break
                sequence += 1
        claimed_paths[workspace_relative] = document.document_id
        return workspace_relative, needs_classification

    def _merge_ref(
        self,
        current: DocumentRef,
        candidate: DocumentRef,
        *,
        prefer_candidate_uri: bool = False,
    ) -> DocumentRef:
        """合并重复发现的引用并优先保留现有路径和来源元数据。"""
        return current.model_copy(
            update={
                "canonical_uri": (
                    candidate.canonical_uri if prefer_candidate_uri else current.canonical_uri
                ),
                "parent_document_id": (current.parent_document_id or candidate.parent_document_id),
                "title_hint": current.title_hint or candidate.title_hint,
                "relative_path_hint": (current.relative_path_hint or candidate.relative_path_hint),
                "metadata": {**candidate.metadata, **current.metadata},
            }
        )


    async def _fetch_one_ref(
        self,
        adapter: SourceAdapter,
        context: AdapterContext,
        ref: DocumentRef,
    ) -> tuple[DocumentRef, ParsedDocument | None, int | None, str | None]:
        """获取并解析一条引用，把异常限制在单文档结果内。"""
        try:
            if ref.source_id != adapter.source_id:
                raise DocumentSyncError(
                    f"引用 source_id {ref.source_id!r} 与 Adapter {adapter.source_id!r} 不一致"
                )
            result = await adapter.fetch(ref, context)
            if result.status_code in {404, 410}:
                return ref, None, result.status_code, None
            document = adapter.parse(ref, result)
            if document.source_id != ref.source_id:
                raise DocumentSyncError("Adapter parse 改变了 source_id 稳定身份")
            return ref, document, None, None
        except Exception as exc:  # noqa: BLE001
            return ref, None, None, f"{type(exc).__name__}: {exc}"

    def _process_discovery_result(
        self,
        adapter: SourceAdapter,
        ref: DocumentRef,
        document: ParsedDocument | None,
        missing_status: int | None,
        error: str | None,
        completed_ids: set[str],
        pending: dict[str, DocumentRef],
        parsed: dict[str, tuple[DocumentRef, ParsedDocument]],
        missing: dict[str, tuple[DocumentRef, int]],
        failed: dict[str, tuple[DocumentRef, str]],
        maximum: int,
    ) -> None:
        """处理单个文档的发现结果，合并规范化 ID 并收集新发现的引用。"""
        completed_ids.add(ref.document_id)
        if missing_status is not None:
            missing[ref.document_id] = (ref, missing_status)
            return
        if error is not None or document is None:
            failed[ref.document_id] = (ref, error or "未知解析错误")
            return
        canonical_id = document.document_id
        completed_ids.add(canonical_id)
        # 同一批次可能同时包含重定向前后的引用；后续结果按稳定
        # canonical document_id 合并，并优先保留已有路径提示。
        pending.pop(canonical_id, None)
        current = parsed.get(canonical_id)
        if current is None:
            parsed[canonical_id] = (ref, document)
        else:
            current_ref, _ = current
            current_score = (
                int(current_ref.document_id == canonical_id),
                int(current_ref.relative_path_hint is not None),
                len(Path(current_ref.relative_path_hint or "").parts),
            )
            candidate_score = (
                int(ref.document_id == canonical_id),
                int(ref.relative_path_hint is not None),
                len(Path(ref.relative_path_hint or "").parts),
            )
            if candidate_score > current_score:
                parsed[canonical_id] = (ref, document)
        for discovered in adapter.discover_refs(document):
            if discovered.source_id != adapter.source_id:
                raise DocumentSyncError(
                    f"发现引用 source_id {discovered.source_id!r} 与 Adapter "
                    f"{adapter.source_id!r} 不一致"
                )
            if discovered.document_id in completed_ids:
                continue
            existing_pending = pending.get(discovered.document_id)
            if existing_pending is None:
                if len(completed_ids) + len(pending) < maximum:
                    pending[discovered.document_id] = discovered
            else:
                pending[discovered.document_id] = self._merge_ref(
                    existing_pending,
                    discovered,
                )

    async def _discover(
        self,
        adapter: SourceAdapter,
        seeds: list[DocumentRef],
        context: AdapterContext,
        *,
        limit: int | None,
    ) -> tuple[
        dict[str, tuple[DocumentRef, ParsedDocument]],
        dict[str, tuple[DocumentRef, int]],
        dict[str, tuple[DocumentRef, str]],
    ]:
        """并发遍历候选引用，分别返回成功、明确缺失和失败结果。"""
        maximum = adapter.max_documents
        if limit is not None:
            maximum = min(maximum, limit) if maximum is not None else limit
        maximum = maximum or 100_000
        pending: dict[str, DocumentRef] = {}
        for ref in seeds:
            if ref.document_id in pending:
                pending[ref.document_id] = self._merge_ref(
                    pending[ref.document_id],
                    ref,
                )
            else:
                pending[ref.document_id] = ref
        completed_ids: set[str] = set()
        parsed: dict[str, tuple[DocumentRef, ParsedDocument]] = {}
        missing: dict[str, tuple[DocumentRef, int]] = {}
        failed: dict[str, tuple[DocumentRef, str]] = {}
        # Adapter.fetch 也会占用来源端的网络/浏览器资源，批次大小不能超过配置的
        # 并发上限；扩大到 concurrency * 4 会同时打开过多页面并触发站点限流。
        batch_size = max(1, self.config.http_defaults.concurrency)

        while pending and len(completed_ids) < maximum:
            batch_ids = [
                document_id for document_id in pending if document_id not in completed_ids
            ][: min(batch_size, maximum - len(completed_ids))]
            if not batch_ids:
                break
            batch = [pending.pop(document_id) for document_id in batch_ids]
            results = await asyncio.gather(
                *(self._fetch_one_ref(adapter, context, ref) for ref in batch)
            )
            for ref, document, missing_status, error in results:
                self._process_discovery_result(
                    adapter,
                    ref,
                    document,
                    missing_status,
                    error,
                    completed_ids,
                    pending,
                    parsed,
                    missing,
                    failed,
                    maximum,
                )
        return parsed, missing, failed

    def _decide_document(
        self,
        *,
        source: SourceConfig,
        adapter: SourceAdapter,
        ref: DocumentRef,
        document: ParsedDocument,
        old_state: SyncStateEntry | None,
        claimed_paths: dict[str, str],
        run_id: str,
    ) -> tuple[DocumentManifestEntry, SyncStateEntry, JournalAction | None]:
        """对一个成功解析的文档计算操作、下一状态和可恢复写入动作。"""
        workspace_relative, needs_classification = self._choose_path(
            source,
            adapter,
            document,
            ref,
            old_state,
            claimed_paths,
        )
        target = self._target_for_relative(workspace_relative)
        current_file_hash = sha256_file(target)
        new_content_hash = sha256_text(document.normalized_content)
        proposed_file_hash = sha256_text(document.artifact_content)
        operation = SyncOperation.ADDED
        reason: str | None = None
        state_file_hash = proposed_file_hash
        if old_state is None and target.exists():
            try:
                local_text = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                local_text = ""
            if _legacy_equivalent(document.normalized_content, local_text):
                operation = SyncOperation.UNCHANGED
                state_file_hash = current_file_hash or proposed_file_hash
                reason = "首次基线与现有 Markdown 正文语义一致"
            else:
                operation = SyncOperation.UPDATED
        elif old_state is not None:
            if old_state.content_hash != new_content_hash:
                operation = SyncOperation.UPDATED
            elif old_state.canonical_uri != document.canonical_uri:
                operation = SyncOperation.MOVED
            elif current_file_hash == proposed_file_hash:
                operation = SyncOperation.UNCHANGED
                state_file_hash = proposed_file_hash
                reason = "本地文件已与来源制品一致"
            elif current_file_hash != old_state.file_hash:
                if self.config.policies.overwrite_local_changes:
                    operation = SyncOperation.RESTORED
                else:
                    operation = SyncOperation.FAILED
                    state_file_hash = old_state.file_hash
                    reason = "本地文件发生修改且 overwrite_local_changes=false"
            else:
                operation = SyncOperation.UNCHANGED
                state_file_hash = current_file_hash or old_state.file_hash

        next_state = SyncStateEntry(
            source_id=source.id,
            document_id=document.document_id,
            canonical_uri=document.canonical_uri,
            relative_path=workspace_relative,
            content_hash=new_content_hash,
            file_hash=state_file_hash,
            last_seen_at=datetime.now(UTC),
            missing_count=0,
            lifecycle_status=LifecycleStatus.ACTIVE,
            metadata=document.metadata,
        )
        manifest = DocumentManifestEntry(
            source_id=source.id,
            adapter_type=adapter.adapter_type,
            document_id=document.document_id,
            external_id=document.external_id,
            canonical_uri=document.canonical_uri,
            relative_path=workspace_relative,
            operation=operation,
            old_content_hash=old_state.content_hash if old_state else None,
            new_content_hash=new_content_hash,
            old_file_hash=current_file_hash,
            new_file_hash=(
                proposed_file_hash
                if operation
                in {
                    SyncOperation.ADDED,
                    SyncOperation.UPDATED,
                    SyncOperation.RESTORED,
                    SyncOperation.MOVED,
                }
                else state_file_hash
            ),
            needs_classification=needs_classification,
            reason=reason,
            metadata=document.metadata,
        )
        if operation == SyncOperation.FAILED and old_state is not None:
            return manifest, old_state, None
        if operation not in {
            SyncOperation.ADDED,
            SyncOperation.UPDATED,
            SyncOperation.RESTORED,
            SyncOperation.MOVED,
        }:
            return manifest, next_state, None
        staging = self.storage.stage_artifact(
            run_id,
            source.id,
            workspace_relative,
            document.artifact_content,
        )
        backup = (
            self.storage.backup_path(run_id, source.id, workspace_relative)
            if target.exists()
            else None
        )
        next_state.file_hash = proposed_file_hash
        action = JournalAction(
            source_id=source.id,
            adapter_type=adapter.adapter_type,
            document_id=document.document_id,
            operation=operation,
            relative_path=workspace_relative,
            staging_path=str(staging),
            target_path=str(target),
            backup_path=str(backup) if backup else None,
            state_entry=next_state,
        )
        return manifest, next_state, action

    def _missing_document(
        self,
        source: SourceConfig,
        adapter: SourceAdapter,
        ref: DocumentRef,
        old_state: SyncStateEntry | None,
        status_code: int,
    ) -> tuple[DocumentManifestEntry, SyncStateEntry | None]:
        """累计明确 404/410 的缺失次数并保留旧文档。"""
        if old_state is None:
            return (
                DocumentManifestEntry(
                    source_id=source.id,
                    adapter_type=adapter.adapter_type,
                    document_id=ref.document_id,
                    canonical_uri=ref.canonical_uri,
                    relative_path=ref.relative_path_hint,
                    operation=SyncOperation.MISSING,
                    missing_count=1,
                    reason=f"HTTP {status_code}，尚无可保留状态",
                    metadata=ref.metadata,
                ),
                None,
            )
        missing_count = old_state.missing_count + 1
        operation = (
            SyncOperation.ARCHIVED_CANDIDATE
            if missing_count >= self.config.policies.missing_threshold
            else SyncOperation.MISSING
        )
        lifecycle = (
            LifecycleStatus.ARCHIVED_CANDIDATE
            if operation == SyncOperation.ARCHIVED_CANDIDATE
            else LifecycleStatus.MISSING
        )
        next_state = old_state.model_copy(
            update={
                "missing_count": missing_count,
                "lifecycle_status": lifecycle,
            }
        )
        return (
            DocumentManifestEntry(
                source_id=source.id,
                adapter_type=adapter.adapter_type,
                document_id=ref.document_id,
                canonical_uri=ref.canonical_uri,
                relative_path=old_state.relative_path,
                operation=operation,
                old_content_hash=old_state.content_hash,
                old_file_hash=old_state.file_hash,
                missing_count=missing_count,
                reason=f"连续 {missing_count} 次明确 HTTP {status_code}，保留旧文件",
                metadata=old_state.metadata,
            ),
            next_state,
        )

    async def _execute_source(
        self,
        source: SourceConfig,
        adapter: SourceAdapter,
        context: AdapterContext,
        *,
        run_id: str,
        limit: int | None,
        batch_size: int | None = None,
        resume_from: str | None = None,
    ) -> SourceExecution:
        """执行单个 source 的发现、决策和 staging。"""
        target_directory = self.config.source_target(source)
        bootstrap_refs = adapter.bootstrap(target_directory)
        state = self.storage.load_state(source.id)
        old_entries = self._reconcile_state_entries(
            source,
            state.entries if state else [],
            bootstrap_refs,
        )
        refs_by_id = {ref.document_id: ref for ref in bootstrap_refs}
        for entry in old_entries.values():
            try:
                target_relative = (
                    Path(entry.relative_path)
                    .relative_to(target_directory.relative_to(self.config.workspace_root))
                    .as_posix()
                )
            except ValueError:
                target_relative = None
            state_ref = DocumentRef(
                source_id=source.id,
                document_id=entry.document_id,
                canonical_uri=entry.canonical_uri,
                relative_path_hint=target_relative,
                metadata=entry.metadata,
            )
            if entry.document_id in refs_by_id:
                refs_by_id[entry.document_id] = self._merge_ref(
                    refs_by_id[entry.document_id],
                    state_ref,
                )
            else:
                refs_by_id[entry.document_id] = state_ref
        for ref in await adapter.initial_refs():
            if ref.document_id in refs_by_id:
                refs_by_id[ref.document_id] = self._merge_ref(
                    refs_by_id[ref.document_id],
                    ref,
                    prefer_candidate_uri=True,
                )
            else:
                refs_by_id[ref.document_id] = ref

        # Sort candidate references lexicographically by document_id
        sorted_refs = sorted(refs_by_id.values(), key=lambda r: r.document_id)
        if resume_from:
            sorted_refs = [ref for ref in sorted_refs if ref.document_id >= resume_from]
        if batch_size is not None:
            sorted_refs = sorted_refs[:batch_size]

        discover_limit = limit
        if batch_size is not None:
            if discover_limit is None:
                discover_limit = batch_size
            else:
                discover_limit = min(discover_limit, batch_size)

        parsed, missing, failed = await self._discover(
            adapter,
            sorted_refs,
            context,
            limit=discover_limit,
        )
        documents: list[DocumentManifestEntry] = []
        state_entries = dict(old_entries)
        actions: list[JournalAction] = []
        errors: list[str] = []
        identity_drifts: list[str] = []
        claimed_paths = {entry.relative_path: entry.document_id for entry in old_entries.values()}
        for document_id, (ref, document) in sorted(parsed.items()):
            try:
                if ref.document_id != document.document_id:
                    identity_drifts.append(
                        f"{source.id}/{ref.document_id} → {document.document_id}"
                    )
                old_state = old_entries.get(document_id)
                if old_state is None:
                    matching_entries = [
                        entry
                        for entry in old_entries.values()
                        if entry.canonical_uri in {ref.canonical_uri, document.canonical_uri}
                    ]
                    if matching_entries:
                        old_state = max(
                            matching_entries,
                            key=lambda entry: self._state_entry_priority(source, entry, None),
                        )
                manifest, next_state, action = self._decide_document(
                    source=source,
                    adapter=adapter,
                    ref=ref,
                    document=document,
                    old_state=old_state,
                    claimed_paths=claimed_paths,
                    run_id=run_id,
                )
            except DocumentSyncError as exc:
                manifest = DocumentManifestEntry(
                    source_id=source.id,
                    adapter_type=adapter.adapter_type,
                    document_id=document_id,
                    canonical_uri=document.canonical_uri,
                    operation=SyncOperation.FAILED,
                    reason=str(exc),
                    metadata=document.metadata,
                )
                errors.append(f"{source.id}/{document_id}: {exc}")
            else:
                # 身份归一后删除同一 canonical URI 的旧别名，避免 state 再次
                # 为同一来源建立两个 document_id。
                for old_id, old_entry in list(state_entries.items()):
                    if (
                        old_id != document_id
                        and old_entry.canonical_uri == next_state.canonical_uri
                    ):
                        state_entries.pop(old_id, None)
                state_entries[document_id] = next_state
                if action is not None:
                    actions.append(action)
            documents.append(manifest)
        for document_id, (ref, status_code) in sorted(missing.items()):
            manifest, next_state = self._missing_document(
                source,
                adapter,
                ref,
                old_entries.get(document_id),
                status_code,
            )
            documents.append(manifest)
            if next_state is not None:
                state_entries[document_id] = next_state
        for document_id, (ref, error) in sorted(failed.items()):
            relative_path = (
                old_entries[document_id].relative_path
                if document_id in old_entries
                else ref.relative_path_hint
            )
            documents.append(
                DocumentManifestEntry(
                    source_id=source.id,
                    adapter_type=adapter.adapter_type,
                    document_id=document_id,
                    canonical_uri=ref.canonical_uri,
                    relative_path=relative_path,
                    operation=SyncOperation.FAILED,
                    reason=error,
                    metadata=ref.metadata,
                )
            )
            errors.append(f"{source.id}/{document_id}: {error}")
        status = "partial" if errors else "succeeded"
        return SourceExecution(
            source=source,
            adapter=adapter,
            documents=documents,
            state_entries=state_entries,
            actions=actions,
            errors=errors,
            identity_drifts=identity_drifts,
            status=status,
        )

    def _apply_executions(
        self,
        run_id: str,
        executions: list[SourceExecution],
    ) -> tuple[list[str], list[str]]:
        """创建 journal、应用所有有效动作并保存成功状态。"""
        actions = [action for execution in executions for action in execution.actions]
        journal = ApplyJournal(
            run_id=run_id,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            actions=actions,
        )
        self.storage.save_journal(journal)
        updated: list[str] = []
        errors: list[str] = []
        failed_ids: set[tuple[str, str]] = set()
        for action in journal.actions:
            try:
                self.storage.apply_action(action, run_id)
            except Exception as exc:  # noqa: BLE001
                failed_ids.add((action.source_id, action.document_id))
                for execution in executions:
                    if execution.source.id == action.source_id:
                        execution.status = "partial"
                errors.append(
                    f"{action.source_id}/{action.document_id} 写入失败: {type(exc).__name__}: {exc}"
                )
            else:
                action.completed = True
                updated.append(action.relative_path)
            self.storage.save_journal(journal)
        for execution in executions:
            current_state = self.storage.load_state(execution.source.id)
            previous_entries = {
                entry.document_id: entry
                for entry in (current_state.entries if current_state else [])
            }
            previous_by_uri = {
                entry.canonical_uri: entry
                for entry in (current_state.entries if current_state else [])
            }
            persisted: dict[str, SyncStateEntry] = {}
            for document_id, entry in execution.state_entries.items():
                if (execution.source.id, document_id) not in failed_ids:
                    persisted[document_id] = entry
                    continue
                previous = previous_entries.get(document_id) or previous_by_uri.get(
                    entry.canonical_uri
                )
                if previous is not None:
                    persisted[document_id] = previous.model_copy(
                        update={"document_id": document_id}
                    )
            self.storage.save_state(
                SourceState(
                    source_id=execution.source.id,
                    updated_at=datetime.now(UTC),
                    entries=sorted(
                        persisted.values(),
                        key=lambda item: item.document_id,
                    ),
                )
            )
            for document in execution.documents:
                if (execution.source.id, document.document_id) in failed_ids:
                    document.operation = SyncOperation.FAILED
                    document.reason = "候选文件写入失败，旧文件和旧状态已保留"
        return sorted(updated), errors

    async def run(
        self,
        mode: str,
        *,
        apply: bool,
        selected_source_ids: set[str] | None = None,
        limit: int | None = None,
        trigger: str = "manual",
        batch_size: int | None = None,
        resume_from: str | None = None,
    ) -> tuple[RunManifest, Path]:
        """执行 bootstrap/sync，保存 manifest 并返回路径。"""
        if mode not in {"bootstrap", "sync"}:
            raise ValueError(f"不支持的同步 mode: {mode}")
        if limit is not None and limit <= 0:
            raise ValueError("limit 必须大于 0")
        if batch_size is not None and batch_size <= 0:
            raise ValueError("batch-size 必须大于 0")
        self.storage.ensure_directories()
        enabled_sources = [
            source
            for source in self.config.sources
            if source.enabled and (selected_source_ids is None or source.id in selected_source_ids)
        ]
        if selected_source_ids:
            missing_ids = selected_source_ids - {source.id for source in enabled_sources}
            if missing_ids:
                raise ValueError("指定 source 不存在或未启用: " + ", ".join(sorted(missing_ids)))
        run_id = str(uuid4())
        started_at = datetime.now(UTC)
        executions: list[SourceExecution] = []
        run_errors: list[str] = []
        with self.storage.lock():
            async with HttpFetchClient(
                self.config.http_defaults,
                self.config.policies.redirects,
            ) as http:
                context = AdapterContext(run_id=run_id, http=http)
                for source in enabled_sources:
                    adapter = AdapterFactory.create(source)
                    try:
                        execution = await self._execute_source(
                            source,
                            adapter,
                            context,
                            run_id=run_id,
                            limit=limit,
                            batch_size=batch_size,
                            resume_from=resume_from,
                        )
                    except Exception as exc:  # noqa: BLE001
                        run_errors.append(f"source {source.id!r} 失败: {type(exc).__name__}: {exc}")
                        executions.append(
                            SourceExecution(
                                source=source,
                                adapter=adapter,
                                documents=[],
                                state_entries={},
                                actions=[],
                                errors=[run_errors[-1]],
                                identity_drifts=[],
                                status="failed",
                            )
                        )
                    else:
                        executions.append(execution)
                        run_errors.extend(execution.errors)
                    finally:
                        await adapter.aclose()
            all_documents = [
                document for execution in executions for document in execution.documents
            ]
            change_count = sum(
                document.operation
                in {
                    SyncOperation.ADDED,
                    SyncOperation.UPDATED,
                    SyncOperation.RESTORED,
                    SyncOperation.MOVED,
                }
                for document in all_documents
            )
            large_change = bool(all_documents) and (
                change_count / len(all_documents) >= self.config.policies.large_change.warning_ratio
            )
            failed_count = sum(
                document.operation == SyncOperation.FAILED for document in all_documents
            )
            source_failures = sum(execution.status == "failed" for execution in executions)
            failure_ratio = (failed_count + source_failures) / max(
                len(all_documents) + source_failures,
                1,
            )
            apply_block_reasons: list[str] = []
            if run_errors and not self.config.policies.apply_valid_changes_on_partial_run:
                apply_block_reasons.append("运行存在错误且配置禁止部分应用")
            if (
                run_errors
                and self.config.policies.partial_run.block_apply
                and failure_ratio > self.config.policies.partial_run.max_failure_ratio
            ):
                apply_block_reasons.append(
                    "失败比例 "
                    f"{failure_ratio:.1%} 超过阈值 "
                    f"{self.config.policies.partial_run.max_failure_ratio:.1%}"
                )
            if apply and apply_block_reasons:
                run_errors.append("安全闸门阻止落盘：" + "；".join(apply_block_reasons))
            should_apply = (
                apply
                and not (large_change and self.config.policies.large_change.block_apply)
                and not apply_block_reasons
            )
            updated_markdown: list[str] = []
            if should_apply:
                updated_markdown, apply_errors = self._apply_executions(
                    run_id,
                    executions,
                )
                run_errors.extend(apply_errors)
            source_results = []
            for execution in executions:
                stats = RunStats.from_documents(execution.documents, sources=1)
                source_results.append(
                    SourceRunResult(
                        source_id=execution.source.id,
                        adapter_type=execution.adapter.adapter_type,
                        status=execution.status,
                        stats=stats.model_dump(exclude={"sources"}),
                    )
                )
            status = "succeeded"
            if run_errors:
                status = "partial" if all_documents else "failed"
            manifest = RunManifest(
                run_id=run_id,
                mode=mode,
                trigger=trigger,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                status=status,
                large_change=large_change,
                stats=RunStats.from_documents(
                    all_documents,
                    sources=len(enabled_sources),
                ),
                updated_markdown=updated_markdown,
                source_results=source_results,
                documents=all_documents,
                errors=run_errors,
            )
            manifest_path = self.storage.write_manifest(manifest)
            self.storage.cleanup_retention()
        return manifest, manifest_path

    async def resume(
        self,
        run_id: str,
        *,
        trigger: str = "manual",
    ) -> tuple[RunManifest, Path]:
        """继续 journal 中未完成的文件动作并合并 source 状态。"""
        self.storage.ensure_directories()
        started_at = datetime.now(UTC)
        errors: list[str] = []
        updated: list[str] = []
        documents: list[DocumentManifestEntry] = []
        with self.storage.lock():
            journal = self.storage.load_journal(run_id)
            state_updates: dict[str, dict[str, SyncStateEntry]] = {}
            for action in journal.actions:
                if action.completed:
                    state_updates.setdefault(action.source_id, {})[action.document_id] = (
                        action.state_entry
                    )
                    continue
                try:
                    self.storage.apply_action(action, run_id)
                except Exception as exc:  # noqa: BLE001
                    errors.append(
                        f"{action.source_id}/{action.document_id}: {type(exc).__name__}: {exc}"
                    )
                    operation = SyncOperation.FAILED
                else:
                    action.completed = True
                    updated.append(action.relative_path)
                    state_updates.setdefault(action.source_id, {})[action.document_id] = (
                        action.state_entry
                    )
                    operation = action.operation
                documents.append(
                    DocumentManifestEntry(
                        source_id=action.source_id,
                        adapter_type=action.adapter_type,
                        document_id=action.document_id,
                        canonical_uri=action.state_entry.canonical_uri,
                        relative_path=action.relative_path,
                        operation=operation,
                        new_content_hash=action.state_entry.content_hash,
                        new_file_hash=action.state_entry.file_hash,
                        reason=errors[-1] if operation == SyncOperation.FAILED else None,
                        metadata=action.state_entry.metadata,
                    )
                )
                self.storage.save_journal(journal)
            for source_id, entries in state_updates.items():
                state = self.storage.load_state(source_id)
                merged = {entry.document_id: entry for entry in (state.entries if state else [])}
                merged.update(entries)
                self.storage.save_state(
                    SourceState(
                        source_id=source_id,
                        updated_at=datetime.now(UTC),
                        entries=sorted(
                            merged.values(),
                            key=lambda item: item.document_id,
                        ),
                    )
                )
            source_types = {(document.source_id, document.adapter_type) for document in documents}
            manifest = RunManifest(
                run_id=str(uuid4()),
                mode="resume",
                trigger=trigger,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                status="partial" if errors else "succeeded",
                large_change=False,
                stats=RunStats.from_documents(
                    documents,
                    sources=len({item[0] for item in source_types}),
                ),
                updated_markdown=sorted(updated),
                source_results=[
                    SourceRunResult(
                        source_id=source_id,
                        adapter_type=adapter_type,
                        status="partial" if errors else "succeeded",
                        stats={},
                    )
                    for source_id, adapter_type in sorted(source_types)
                ],
                documents=documents,
                errors=errors,
            )
            manifest_path = self.storage.write_manifest(manifest)
        return manifest, manifest_path
