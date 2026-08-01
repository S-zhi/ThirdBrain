"""独立 document_sync.yaml 的严格配置模型与加载器。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

import yaml
from src.doc_sync.errors import SyncConfigError


class StrictConfigModel(BaseModel):
    """为所有同步配置拒绝未知字段。"""

    model_config = ConfigDict(extra="forbid")


class RetryConfig(StrictConfigModel):
    """定义 HTTP 重试次数、退避和可重试状态码。"""

    max_attempts: int = Field(default=4, ge=1)
    initial_backoff_seconds: float = Field(default=1, gt=0)
    max_backoff_seconds: float = Field(default=30, gt=0)
    jitter_ratio: float = Field(default=0.2, ge=0, le=1)
    retry_status_codes: list[int] = Field(
        default_factory=lambda: [429, 500, 502, 503, 504],
        min_length=1,
    )

    @field_validator("retry_status_codes")
    @classmethod
    def validate_retry_status_codes(cls, value: list[int]) -> list[int]:
        """保证重试状态码处于合法 HTTP 范围且不重复。"""
        if any(status < 100 or status > 599 for status in value):
            raise ValueError("retry_status_codes 必须是 100 到 599 的 HTTP 状态码")
        if len(value) != len(set(value)):
            raise ValueError("retry_status_codes 不能重复")
        return value


class HttpDefaultsConfig(StrictConfigModel):
    """定义所有 HTTP Adapter 共用的请求策略。"""

    user_agent: str = Field(default="rag-with-cold-api-doc-sync/0.1", min_length=1)
    concurrency: int = Field(default=4, ge=1)
    requests_per_second: float = Field(default=2, gt=0)
    timeout_seconds: float = Field(default=30, gt=0)
    max_response_bytes: int = Field(default=10 * 1024 * 1024, ge=1)
    respect_robots_txt: bool = True
    retry: RetryConfig = Field(default_factory=RetryConfig)


class RuntimeConfig(StrictConfigModel):
    """定义运行时状态、暂存、备份和锁的根目录。"""

    root_directory: Path = Path("./data/doc_sync")
    retention_days: int = Field(default=30, ge=1)
    lock_timeout_seconds: float = Field(default=0, ge=0)


class RedirectPolicy(StrictConfigModel):
    """定义安全重定向的最大次数和跨域策略。"""

    max_redirects: int = Field(default=3, ge=0)
    allow_cross_host: bool = False


class LargeChangePolicy(StrictConfigModel):
    """定义大批量变化的告警阈值与是否阻断。"""

    warning_ratio: float = Field(default=0.1, ge=0, le=1)
    block_apply: bool = False


class PartialRunPolicy(StrictConfigModel):
    """定义部分失败运行的安全落盘阈值。"""

    max_failure_ratio: float = Field(default=0.25, ge=0, le=1)
    block_apply: bool = True


class PathCollisionPolicy(StrictConfigModel):
    """定义新文档目标路径冲突的处理方式。"""

    strategy: Literal["append_document_id", "fail"] = "append_document_id"


class SyncPolicies(StrictConfigModel):
    """定义来源无关的同步决策策略。"""

    missing_threshold: int = Field(default=3, ge=1)
    overwrite_local_changes: bool = True
    apply_valid_changes_on_partial_run: bool = True
    redirects: RedirectPolicy = Field(default_factory=RedirectPolicy)
    large_change: LargeChangePolicy = Field(default_factory=LargeChangePolicy)
    partial_run: PartialRunPolicy = Field(default_factory=PartialRunPolicy)
    path_collision: PathCollisionPolicy = Field(default_factory=PathCollisionPolicy)


class AdapterDefinition(StrictConfigModel):
    """定义 Adapter 类型和仅由对应 Adapter 解释的 options。"""

    type: str = Field(min_length=1)
    options: dict[str, Any] = Field(default_factory=dict)


class SourceConfig(StrictConfigModel):
    """定义一个可独立启停的文档来源。"""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    enabled: bool = True
    target_directory: Path
    adapter: AdapterDefinition


class DocumentSyncConfig(StrictConfigModel):
    """定义独立文档同步配置文件的完整结构。"""

    schema_version: Literal["1.0"] = "1.0"
    workspace_root: Path = Path("..")
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    http_defaults: HttpDefaultsConfig = Field(default_factory=HttpDefaultsConfig)
    policies: SyncPolicies = Field(default_factory=SyncPolicies)
    sources: list[SourceConfig] = Field(min_length=1)
    config_path: Path | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def validate_unique_source_ids(self) -> Self:
        """保证所有 source id 唯一。"""
        source_ids = [source.id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("sources 中的 id 必须唯一")
        return self

    @property
    def runtime_root(self) -> Path:
        """返回相对于 workspace_root 解析后的运行目录。"""
        return _resolve_workspace_path(self.workspace_root, self.runtime.root_directory)

    def source_target(self, source: SourceConfig) -> Path:
        """返回指定 source 相对于 workspace_root 的目标目录。"""
        return _resolve_workspace_path(self.workspace_root, source.target_directory)


SENSITIVE_EXACT_KEYS = {
    "api_key",
    "authorization",
    "cookie",
    "credential",
    "credentials",
    "password",
    "secret",
    "secret_key",
    "token",
}
SENSITIVE_KEY_PARTS = {
    "authorization",
    "cookie",
    "credential",
    "credentials",
    "password",
    "secret",
    "token",
}


def _resolve_workspace_path(workspace_root: Path, value: Path) -> Path:
    """把配置中的路径稳定解析为 workspace 下的绝对路径。"""
    return value.resolve() if value.is_absolute() else (workspace_root / value).resolve()


def _reject_inline_secrets(value: Any, trail: tuple[str, ...] = ()) -> None:
    """递归拒绝 YAML 中的明文凭证字段，同时允许 *_env 引用。"""
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key).lower()
            normalized_key = re.sub(r"[^a-z0-9]+", "_", key).strip("_")
            key_parts = set(normalized_key.split("_"))
            is_sensitive = normalized_key in SENSITIVE_EXACT_KEYS or bool(
                key_parts & SENSITIVE_KEY_PARTS
            )
            if is_sensitive and not normalized_key.endswith("_env"):
                location = ".".join((*trail, str(raw_key)))
                raise SyncConfigError(
                    f"配置禁止保存明文凭证字段 {location!r}；请改为 *_env 环境变量名"
                )
            _reject_inline_secrets(child, (*trail, str(raw_key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_inline_secrets(child, (*trail, str(index)))


def _validate_directory_layout(config: DocumentSyncConfig) -> None:
    """校验 runtime 与各 source 目标目录不重叠。"""
    runtime_root = config.runtime_root
    if not runtime_root.is_relative_to(config.workspace_root):
        raise SyncConfigError("runtime.root_directory 必须位于 workspace_root 内")
    targets = [(source.id, config.source_target(source)) for source in config.sources]
    for source_id, target in targets:
        if not target.is_relative_to(config.workspace_root):
            raise SyncConfigError(
                f"source {source_id!r} 的 target_directory 必须位于 workspace_root 内"
            )
        if target == runtime_root or target.is_relative_to(runtime_root):
            raise SyncConfigError(f"source {source_id!r} 的 target_directory 不能位于 runtime 内")
        if runtime_root.is_relative_to(target):
            raise SyncConfigError(f"runtime 不能位于 source {source_id!r} 的 target_directory 内")
    for index, (left_id, left) in enumerate(targets):
        for right_id, right in targets[index + 1 :]:
            if left == right or left.is_relative_to(right) or right.is_relative_to(left):
                raise SyncConfigError(
                    f"source 目标目录不能重叠: {left_id!r}={left}, {right_id!r}={right}"
                )


def load_document_sync_config(path: str | Path) -> DocumentSyncConfig:
    """读取 YAML、解析 workspace_root 并执行全部通用配置校验。"""
    config_path = Path(path).expanduser().resolve()
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SyncConfigError(f"同步配置不存在: {config_path}") from exc
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise SyncConfigError(f"无法读取同步配置 {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SyncConfigError("document_sync.yaml 顶层必须是 mapping")
    _reject_inline_secrets(raw)
    try:
        config = DocumentSyncConfig.model_validate(raw)
    except ValueError as exc:
        raise SyncConfigError(f"同步配置校验失败: {exc}") from exc
    workspace = config.workspace_root
    if not workspace.is_absolute():
        workspace = (config_path.parent / workspace).resolve()
    config.workspace_root = workspace
    config.config_path = config_path
    _validate_directory_layout(config)
    return config
