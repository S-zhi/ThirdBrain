"""把同一来源 URL 的重复 Markdown 可恢复地隔离，并重建 source state。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from src.doc_sync import load_document_sync_config
from src.doc_sync.adapters import AdapterFactory
from src.doc_sync.models import LifecycleStatus, SourceState, SyncStateEntry
from src.doc_sync.storage import SyncStorage, write_json_atomic


def _sha256_file(path: Path) -> str:
    """计算 Markdown 文件 SHA-256。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    """为没有历史 state 的文件建立保守的本地内容基线。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _relative_to_workspace(path: Path, workspace_root: Path) -> str:
    """把工作区路径转换成 state 使用的 POSIX 路径。"""
    return path.resolve().relative_to(workspace_root.resolve()).as_posix()


def _generated_name(path: Path) -> bool:
    """识别路径碰撞策略生成的 document-id 后缀文件。"""
    return bool(re.search(r"--(?:atlas(?:ascendc|_ascendc)_api|context)", path.stem))


def _select_keeper(
    paths: list[Path],
    *,
    target: Path,
    canonical_document_id: str,
    old_entries: list[SyncStateEntry],
    workspace_root: Path,
) -> Path:
    """优先保留 canonical state、非碰撞后缀且层级最深的文件。"""

    def score(path: Path) -> tuple[int, int, int, str]:
        relative = _relative_to_workspace(path, workspace_root)
        matching_state = [
            entry
            for entry in old_entries
            if entry.relative_path == relative and entry.document_id == canonical_document_id
        ]
        return (
            int(bool(matching_state)),
            int(not _generated_name(path)),
            len(path.relative_to(target).parts),
            # max() 最后按字典序稳定选择，避免依赖目录遍历顺序。
            path.as_posix(),
        )

    return max(paths, key=score)


def _empty_directories(root: Path) -> list[str]:
    """删除 root 下已经没有文件的空目录，并返回相对路径。"""
    removed: list[str] = []
    for directory in sorted(
        (path for path in root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        try:
            directory.rmdir()
        except OSError:
            continue
        removed.append(directory.relative_to(root).as_posix())
    return removed


def run(config_path: Path, source_id: str, *, apply: bool) -> Path:
    """执行重复文件隔离；默认只生成报告，不改动工作区。"""
    config = load_document_sync_config(config_path)
    source = next((item for item in config.sources if item.id == source_id), None)
    if source is None:
        raise ValueError(f"source 不存在: {source_id}")
    adapter = AdapterFactory.create(source)
    target = config.source_target(source)
    storage = SyncStorage(config)
    state = storage.load_state(source.id)
    old_entries = list(state.entries) if state is not None else []

    groups: dict[str, list[Path]] = {}
    source_pattern = getattr(adapter, "_source_pattern", None)
    if source_pattern is None:
        raise ValueError("当前 Adapter 不支持从 Markdown 读取来源 URL")
    for path in sorted(target.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        match = source_pattern.search(text)
        if match is None:
            continue
        uri = adapter.canonicalize_uri(match.group("value").strip())  # type: ignore[attr-defined]
        if adapter.is_allowed_uri(uri):  # type: ignore[attr-defined]
            groups.setdefault(uri, []).append(path)

    duplicate_groups: list[dict[str, object]] = []
    for uri, paths in sorted(groups.items()):
        if len(paths) < 2:
            continue
        document_id = adapter._document_id(uri)  # type: ignore[attr-defined]
        keeper = _select_keeper(
            paths,
            target=target,
            canonical_document_id=document_id,
            old_entries=old_entries,
            workspace_root=config.workspace_root,
        )
        duplicate_groups.append(
            {
                "canonical_uri": uri,
                "document_id": document_id,
                "kept_path": _relative_to_workspace(keeper, config.workspace_root),
                "quarantine_paths": [
                    _relative_to_workspace(path, config.workspace_root)
                    for path in paths
                    if path != keeper
                ],
            }
        )

    run_id = str(uuid4())
    quarantine_root = config.runtime_root / "quarantine" / run_id
    manifest_path = quarantine_root / "quarantine_manifest.json"
    report: dict[str, object] = {
        "schema_version": "1.0",
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "source_id": source.id,
        "mode": "apply" if apply else "dry-run",
        "target_directory": _relative_to_workspace(target, config.workspace_root),
        "duplicate_groups": duplicate_groups,
        "duplicate_files": sum(len(item["quarantine_paths"]) for item in duplicate_groups),
        "state_entries_before": len(old_entries),
    }

    if not apply:
        quarantine_root.mkdir(parents=True, exist_ok=True)
        write_json_atomic(manifest_path, report)
        return manifest_path

    quarantine_root.mkdir(parents=True, exist_ok=False)
    if state is not None:
        shutil.copy2(storage.state_path(source.id), quarantine_root / "state-before.json")

    for group in duplicate_groups:
        for raw_path in group["quarantine_paths"]:
            original = config.workspace_root / str(raw_path)
            destination = quarantine_root / "files" / str(raw_path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(original), str(destination))
    removed_dirs = _empty_directories(target)

    refs = adapter.bootstrap(target)
    if len(refs) != len({ref.document_id for ref in refs}):
        raise ValueError("隔离后仍存在重复 document_id，拒绝重建 state")
    if len(refs) != len({ref.canonical_uri for ref in refs}):
        raise ValueError("隔离后仍存在重复 canonical_uri，拒绝重建 state")

    rebuilt_entries: list[SyncStateEntry] = []
    for ref in refs:
        if not ref.relative_path_hint:
            raise ValueError(f"来源 ref 缺少 relative_path_hint: {ref.document_id}")
        target_path = target / ref.relative_path_hint
        if not target_path.is_file():
            raise ValueError(f"保留文件不存在: {target_path}")
        relative = _relative_to_workspace(target_path, config.workspace_root)
        candidates = [entry for entry in old_entries if entry.canonical_uri == ref.canonical_uri]
        candidate = max(
            candidates,
            key=lambda entry: (
                int(entry.relative_path == relative),
                int(entry.document_id == ref.document_id),
                len(Path(entry.relative_path).parts),
            ),
            default=None,
        )
        local_text = target_path.read_text(encoding="utf-8")
        metadata = {**(candidate.metadata if candidate else {}), **ref.metadata}
        rebuilt_entries.append(
            SyncStateEntry(
                source_id=source.id,
                document_id=ref.document_id,
                canonical_uri=ref.canonical_uri,
                relative_path=relative,
                content_hash=(candidate.content_hash if candidate else _sha256_text(local_text)),
                file_hash=_sha256_file(target_path),
                last_seen_at=(candidate.last_seen_at if candidate else datetime.now(UTC)),
                missing_count=0,
                lifecycle_status=LifecycleStatus.ACTIVE,
                metadata=metadata,
            )
        )
    storage.save_state(
        SourceState(
            source_id=source.id,
            updated_at=datetime.now(UTC),
            entries=sorted(rebuilt_entries, key=lambda entry: entry.document_id),
        )
    )
    report["removed_empty_directories"] = removed_dirs
    report["state_entries_after"] = len(rebuilt_entries)
    report["state_path"] = _relative_to_workspace(storage.state_path(source.id), config.workspace_root)
    write_json_atomic(manifest_path, report)
    return manifest_path


def main(argv: list[str] | None = None) -> int:
    """执行命令行入口。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("configs/document_sync.yaml"))
    parser.add_argument("--source", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    manifest_path = run(args.config, args.source, apply=args.apply)
    print(json.dumps({"manifest_path": str(manifest_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
