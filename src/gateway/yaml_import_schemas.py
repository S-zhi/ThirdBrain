"""YAML 批量导入接口的请求与响应模型。"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from src.gateway.schemas import GatewayError, GatewayModel, NonEmptyString

CollectionName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=63,
        pattern=r"^[a-z][a-z0-9_]{0,62}$",
    ),
]


class YamlImportItemStatus(StrEnum):
    """单个 YAML 文件的 HTTP 响应状态。"""

    INSERTED = "inserted"
    DUPLICATE = "duplicate"
    FAILED = "failed"


class YamlImportBatchStatus(StrEnum):
    """整个 YAML 批次的 HTTP 响应状态。"""

    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


class YamlImportItem(GatewayModel):
    """定义一个 YAML 文件与目标 Collection 的映射。"""

    custom_id: NonEmptyString
    file_path: NonEmptyString
    collection: CollectionName


class BatchYamlImportRequest(GatewayModel):
    """批量 YAML 导入请求。"""

    items: list[YamlImportItem] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_unique_custom_ids(self) -> Self:
        """校验同一批次内的 custom_id 不重复。"""
        custom_ids = [item.custom_id for item in self.items]
        if len(custom_ids) != len(set(custom_ids)):
            raise ValueError("同一批次中的 custom_id 必须唯一")
        return self


class YamlImportResult(GatewayModel):
    """单个 YAML 文件的导入结果。"""

    custom_id: str
    file_path: str
    collection: str
    status: YamlImportItemStatus
    schema_version: str | None = None
    inserted_id: str | None = None
    error: GatewayError | None = None


class BatchYamlImportResponse(GatewayModel):
    """批量 YAML 导入的汇总响应。"""

    request_id: str
    status: YamlImportBatchStatus
    succeeded_count: int = Field(ge=0)
    duplicate_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    results: list[YamlImportResult]
