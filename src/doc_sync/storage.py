"""同步状态、暂存、备份、journal 与原子写入设施。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self

from src.doc_sync.config import DocumentSyncConfig
from src.doc_sync.errors import PathSafetyError, ResumeError, SyncLockError
from src.doc_sync.models import (
    ApplyJournal,
    JournalAction,
    RunManifest,
    SourceState,
)

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


def write_text_atomic(path: Path, content: str) -> None:
    """使用同目录临时文件和 os.replace 原子写入文本。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def write_json_atomic(path: Path, value: Any) -> None:
    """把 Pydantic 模型或普通对象原子写为 UTF-8 JSON。"""
    if hasattr(value, "model_dump_json"):
        content = value.model_dump_json(indent=2)
    else:
        content = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    write_text_atomic(path, content + ("" if content.endswith("\n") else "\n"))


def safe_relative_path(value: str) -> Path:
    """校验状态或 Adapter 返回的是安全相对路径。"""
    path = Path(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise PathSafetyError(f"不安全的相对路径: {value!r}")
    return path


def resolve_under(root: Path, relative_path: str) -> Path:
    """解析并保证结果严格位于指定根目录内。"""
    root = root.resolve()
    resolved = (root / safe_relative_path(relative_path)).resolve()
    if not resolved.is_relative_to(root):
        raise PathSafetyError(f"路径逃逸 {root}: {relative_path!r}")
    return resolved


def _sha256_file(path: Path) -> str:
    """计算内部原子写入校验使用的文件 SHA-256。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class SyncRunLock(AbstractContextManager["SyncRunLock"]):
    """使用 flock 在服务器进程间互斥文档同步任务。"""

    def __init__(self, path: Path, timeout_seconds: float) -> None:
        """保存锁文件路径和最大等待时间。"""
        self._path = path
        self._timeout_seconds = timeout_seconds
        self._handle: Any = None

    def __enter__(self) -> Self:
        """打开并非阻塞地获取文件锁。"""
        if fcntl is None:
            raise SyncLockError("当前平台不支持 fcntl 文件锁")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._path.open("a+", encoding="utf-8")
        deadline = time.monotonic() + self._timeout_seconds
        while True:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    self._handle.close()
                    self._handle = None
                    raise SyncLockError(f"已有同步任务持有锁: {self._path}") from exc
                time.sleep(min(0.2, max(0.01, deadline - time.monotonic())))
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "acquired_at": datetime.now(UTC).isoformat(),
                },
                ensure_ascii=False,
            )
        )
        self._handle.flush()
        return self

    def __exit__(self, *_: object) -> None:
        """释放文件锁并关闭锁文件句柄。"""
        if self._handle is None:
            return
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None


class SyncStorage:
    """集中管理 data/doc_sync 下的所有运行时制品。"""

    def __init__(self, config: DocumentSyncConfig) -> None:
        """根据配置解析并缓存所有运行目录。"""
        self.config = config
        self.root = config.runtime_root
        self.state_directory = self.root / "state"
        self.run_directory = self.root / "runs"
        self.staging_directory = self.root / "staging"
        self.backup_directory = self.root / "backups"
        self.journal_directory = self.root / "journals"
        self.lock_path = self.root / "sync.lock"
        self.latest_path = self.root / "latest.json"

    def ensure_directories(self) -> None:
        """创建同步运行需要的固定目录。"""
        for directory in (
            self.state_directory,
            self.run_directory,
            self.staging_directory,
            self.backup_directory,
            self.journal_directory,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def lock(self) -> SyncRunLock:
        """构造使用配置超时的进程互斥锁。"""
        return SyncRunLock(
            self.lock_path,
            self.config.runtime.lock_timeout_seconds,
        )

    def state_path(self, source_id: str) -> Path:
        """返回一个 source 的持久化状态路径。"""
        safe_id = source_id.replace("/", "_").replace("\\", "_")
        return self.state_directory / f"{safe_id}.json"

    def load_state(self, source_id: str) -> SourceState | None:
        """读取并强校验 source 状态；不存在返回 None。"""
        path = self.state_path(source_id)
        if not path.exists():
            return None
        try:
            return SourceState.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise ResumeError(f"状态文件损坏 {path}: {exc}") from exc

    def save_state(self, state: SourceState) -> None:
        """原子保存一个 source 的完整状态。"""
        write_json_atomic(self.state_path(state.source_id), state)

    def stage_artifact(
        self,
        run_id: str,
        source_id: str,
        workspace_relative_path: str,
        content: str,
    ) -> Path:
        """把候选 Markdown 原子写入当前运行的 staging。"""
        relative = safe_relative_path(workspace_relative_path)
        path = resolve_under(self.staging_directory / run_id / source_id, relative.as_posix())
        write_text_atomic(path, content)
        return path

    def backup_path(
        self,
        run_id: str,
        source_id: str,
        workspace_relative_path: str,
    ) -> Path:
        """返回当前运行中某个目标文件的安全备份路径。"""
        return resolve_under(
            self.backup_directory / run_id / source_id,
            workspace_relative_path,
        )

    def journal_path(self, run_id: str) -> Path:
        """返回指定运行的 apply journal 路径。"""
        return self.journal_directory / f"{run_id}.json"

    def save_journal(self, journal: ApplyJournal) -> None:
        """原子保存 apply journal。"""
        journal.updated_at = datetime.now(UTC)
        write_json_atomic(self.journal_path(journal.run_id), journal)

    def load_journal(self, run_id: str) -> ApplyJournal:
        """读取一个可恢复的 apply journal。"""
        path = self.journal_path(run_id)
        if not path.exists():
            raise ResumeError(f"journal 不存在: {path}")
        try:
            return ApplyJournal.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise ResumeError(f"journal 无法解析 {path}: {exc}") from exc

    def apply_action(self, action: JournalAction, run_id: str) -> None:
        """备份目标并把 staging 文件原子替换到工作区。"""
        staging = Path(action.staging_path).resolve()
        target = Path(action.target_path).resolve()
        if not staging.is_relative_to((self.staging_directory / run_id).resolve()):
            raise PathSafetyError(f"staging 路径不属于当前 run: {staging}")
        if not target.is_relative_to(self.config.workspace_root):
            raise PathSafetyError(f"目标路径不属于 workspace: {target}")
        if not staging.is_file():
            raise ResumeError(f"staging 文件不存在: {staging}")
        if target.is_file() and _sha256_file(target) == action.state_entry.file_hash:
            return
        if target.exists():
            backup = Path(action.backup_path).resolve() if action.backup_path else None
            if backup is None:
                raise ResumeError(f"已有目标缺少 backup_path: {target}")
            if not backup.is_relative_to((self.backup_directory / run_id).resolve()):
                raise PathSafetyError(f"备份路径不属于当前 run: {backup}")
            backup.parent.mkdir(parents=True, exist_ok=True)
            if not backup.exists():
                shutil.copy2(target, backup)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.parent / f".{target.name}.{run_id}.tmp"
        shutil.copyfile(staging, temporary)
        os.replace(temporary, target)

    def write_manifest(self, manifest: RunManifest) -> Path:
        """保存按 run 分区的 manifest 并更新 latest.json。"""
        manifest_path = self.run_directory / manifest.run_id / "manifest.json"
        write_json_atomic(manifest_path, manifest)
        write_json_atomic(self.latest_path, manifest)
        return manifest_path

    def cleanup_retention(self) -> list[Path]:
        """清理超过保留期的运行目录与 journal，永久保留同步 state。"""
        cutoff = datetime.now(UTC) - timedelta(days=self.config.runtime.retention_days)
        removed: list[Path] = []

        # 收集所有配置/默认的 rendered_html 目录
        html_directories: set[Path] = set()
        default_dir = Path("./data/doc_sync/rendered_html")
        html_directories.add(
            default_dir.resolve() if default_dir.is_absolute() else (self.config.workspace_root / default_dir).resolve()
        )
        html_directories.add((self.root / "rendered_html").resolve())
        for source in self.config.sources:
            if hasattr(source, "adapter") and source.adapter.type == "hiascend":
                options = source.adapter.options
                if isinstance(options, dict):
                    browser_opts = options.get("browser")
                    if isinstance(browser_opts, dict):
                        html_dir_raw = browser_opts.get("rendered_html_directory")
                        if html_dir_raw:
                            html_dir = Path(html_dir_raw)
                            resolved_dir = html_dir.resolve() if html_dir.is_absolute() else (self.config.workspace_root / html_dir).resolve()
                            html_directories.add(resolved_dir)

        # 清理超期的 html 缓存文件及空目录
        for html_dir in html_directories:
            if not html_dir.exists():
                continue
            for root, dirs, files in os.walk(html_dir, topdown=False):
                for filename in files:
                    filepath = Path(root) / filename
                    try:
                        modified = datetime.fromtimestamp(filepath.stat().st_mtime, tz=UTC)
                        if modified < cutoff:
                            resolved_file = filepath.resolve()
                            if resolved_file.is_relative_to(html_dir.resolve()):
                                resolved_file.unlink()
                                removed.append(resolved_file)
                    except OSError:
                        pass
                # 自底向上清理空子目录
                for dirname in dirs:
                    dirpath = Path(root) / dirname
                    try:
                        resolved_dir = dirpath.resolve()
                        if resolved_dir.is_relative_to(html_dir.resolve()):
                            if not any(dirpath.iterdir()):
                                dirpath.rmdir()
                                removed.append(resolved_dir)
                    except OSError:
                        pass

        for parent in (
            self.run_directory,
            self.staging_directory,
            self.backup_directory,
        ):
            if not parent.exists():
                continue
            for candidate in parent.iterdir():
                if not candidate.is_dir():
                    continue
                modified = datetime.fromtimestamp(candidate.stat().st_mtime, tz=UTC)
                if modified >= cutoff:
                    continue
                resolved = candidate.resolve()
                if not resolved.is_relative_to(parent.resolve()):
                    continue
                shutil.rmtree(resolved)
                removed.append(resolved)
        if self.journal_directory.exists():
            for journal in self.journal_directory.glob("*.json"):
                modified = datetime.fromtimestamp(journal.stat().st_mtime, tz=UTC)
                if modified >= cutoff:
                    continue
                resolved = journal.resolve()
                if not resolved.is_relative_to(self.journal_directory.resolve()):
                    continue
                resolved.unlink()
                removed.append(resolved)
        return removed
