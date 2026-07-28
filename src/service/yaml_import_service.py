"""批量读取 API 文档 YAML 并写入 MongoDB 的 Service。"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.core import YamlDocumentError, read_yaml_document
from src.dao.mongo import (
    DAOUnavailableError,
    DAOValidationError,
    YamlDocumentDAO,
    validate_collection_name,
)
from src.dao.mongo.exceptions import DAOError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ALLOWED_ROOTS = [
    PROJECT_ROOT / "yaml",
    PROJECT_ROOT / "ingest" / "output",
]


class YamlImportSettings(BaseSettings):
    """控制 Server 可读取目录和单文件大小上限。"""

    model_config = SettingsConfigDict(
        env_prefix="RAG_YAML_IMPORT_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    allowed_roots: list[Path] = Field(default_factory=lambda: list(DEFAULT_ALLOWED_ROOTS))
    max_file_bytes: int = Field(default=10 * 1024 * 1024, ge=1)


class YamlImportItemStatus(StrEnum):
    """单个 YAML 文件的导入状态。"""

    INSERTED = "inserted"
    DUPLICATE = "duplicate"
    FAILED = "failed"


class YamlImportBatchStatus(StrEnum):
    """整个 YAML 批次的汇总状态。"""

    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class YamlImportCommand:
    """描述一个文件到目标 MongoDB Collection 的映射。"""

    custom_id: str
    file_path: str
    collection: str


@dataclass(frozen=True, slots=True)
class YamlImportItemError:
    """描述单个文件的稳定错误码和信息。"""

    code: str
    message: str


@dataclass(frozen=True, slots=True)
class YamlImportItemResult:
    """描述单个文件的写入结果。"""

    custom_id: str
    file_path: str
    collection: str
    status: YamlImportItemStatus
    schema_version: str | None = None
    inserted_id: str | None = None
    error: YamlImportItemError | None = None


@dataclass(frozen=True, slots=True)
class BatchYamlImportResult:
    """描述批量导入的汇总状态和有序单项结果。"""

    status: YamlImportBatchStatus
    succeeded_count: int
    duplicate_count: int
    failed_count: int
    results: tuple[YamlImportItemResult, ...]


class YamlImportService:
    """执行路径校验、YAML 解析和 MongoDB 单条写入。"""

    def __init__(
        self,
        dao: YamlDocumentDAO,
        settings: YamlImportSettings | None = None,
    ) -> None:
        """注入 YAML DAO 和可选的导入配置。"""
        self._dao = dao
        self._settings = settings or YamlImportSettings()
        self._allowed_roots = tuple(
            root.expanduser().resolve() for root in self._settings.allowed_roots
        )

    def _resolve_source_path(self, source: str) -> Path:
        """解析文件真实路径并确保它位于配置允许的目录内。"""
        candidate = Path(source).expanduser()
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise YamlDocumentError("FILE_NOT_FOUND", "YAML 文件不存在") from exc
        except OSError as exc:
            raise YamlDocumentError(
                "FILE_READ_ERROR",
                f"无法解析 YAML 文件路径: {type(exc).__name__}",
            ) from exc
        if not resolved.is_file():
            raise YamlDocumentError("INVALID_FILE_TYPE", "输入路径必须是普通文件")
        if resolved.suffix.lower() not in {".yaml", ".yml"}:
            raise YamlDocumentError(
                "INVALID_FILE_TYPE",
                "输入文件扩展名必须是 .yaml 或 .yml",
            )
        if not any(resolved.is_relative_to(root) for root in self._allowed_roots):
            raise YamlDocumentError(
                "PATH_NOT_ALLOWED",
                "YAML 文件不在 Server 允许读取的目录中",
            )
        return resolved

    @staticmethod
    def _failed_result(
        command: YamlImportCommand,
        *,
        code: str,
        message: str,
        file_path: str | None = None,
        schema_version: str | None = None,
    ) -> YamlImportItemResult:
        """构造不泄露 YAML 正文的单项失败结果。"""
        return YamlImportItemResult(
            custom_id=command.custom_id,
            file_path=file_path or command.file_path,
            collection=command.collection,
            status=YamlImportItemStatus.FAILED,
            schema_version=schema_version,
            error=YamlImportItemError(code=code, message=message),
        )

    async def _import_one(self, command: YamlImportCommand) -> YamlImportItemResult:
        """完成一个 YAML 文件的校验、解析和单条写入。"""
        try:
            validate_collection_name(command.collection)
        except DAOValidationError as exc:
            return self._failed_result(
                command,
                code="INVALID_COLLECTION_NAME",
                message=str(exc),
            )
        try:
            source_path = self._resolve_source_path(command.file_path)
        except YamlDocumentError as exc:
            return self._failed_result(command, code=exc.code, message=str(exc))
        try:
            parsed = await asyncio.to_thread(
                read_yaml_document,
                source_path,
                max_file_bytes=self._settings.max_file_bytes,
            )
        except YamlDocumentError as exc:
            return self._failed_result(
                command,
                code=exc.code,
                message=str(exc),
                file_path=str(source_path),
            )
        except OSError as exc:
            return self._failed_result(
                command,
                code="FILE_READ_ERROR",
                message=f"无法读取 YAML 文件: {type(exc).__name__}",
                file_path=str(source_path),
            )
        try:
            insert_result = await self._dao.insert_one(
                command.collection,
                parsed.payload,
            )
        except DAOUnavailableError as exc:
            return self._failed_result(
                command,
                code="MONGO_UNAVAILABLE",
                message=str(exc),
                file_path=str(source_path),
                schema_version=parsed.schema_version,
            )
        except DAOError as exc:
            return self._failed_result(
                command,
                code="MONGO_WRITE_FAILED",
                message=str(exc),
                file_path=str(source_path),
                schema_version=parsed.schema_version,
            )
        return YamlImportItemResult(
            custom_id=command.custom_id,
            file_path=str(source_path),
            collection=command.collection,
            status=(
                YamlImportItemStatus.INSERTED
                if insert_result.inserted
                else YamlImportItemStatus.DUPLICATE
            ),
            schema_version=parsed.schema_version,
            inserted_id=insert_result.document_id,
        )

    async def import_batch(
        self,
        commands: Sequence[YamlImportCommand],
    ) -> BatchYamlImportResult:
        """按请求顺序批量导入，并将每个文件的失败限制在单项内。"""
        results = tuple([await self._import_one(command) for command in commands])
        failed_count = sum(
            result.status == YamlImportItemStatus.FAILED for result in results
        )
        duplicate_count = sum(
            result.status == YamlImportItemStatus.DUPLICATE for result in results
        )
        succeeded_count = len(results) - failed_count
        if failed_count == 0:
            status = YamlImportBatchStatus.SUCCEEDED
        elif succeeded_count == 0:
            status = YamlImportBatchStatus.FAILED
        else:
            status = YamlImportBatchStatus.PARTIAL
        return BatchYamlImportResult(
            status=status,
            succeeded_count=succeeded_count,
            duplicate_count=duplicate_count,
            failed_count=failed_count,
            results=results,
        )
