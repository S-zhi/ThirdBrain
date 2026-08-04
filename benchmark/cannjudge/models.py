"""CANN Judge 算子开发场景的数据模型。"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    """拒绝未声明字段，避免数据源变更被静默吞掉。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceRef(StrictModel):
    """场景在 CANN Judge 上的可追溯来源。"""

    provider: Literal["cannjudge"] = "cannjudge"
    base_url: str
    group_id: str
    contest_id: str
    contest_name: str
    problem_id: str
    problem_name: str
    problem_url: str


class JudgeSpec(StrictModel):
    """远端判题器契约。"""

    kind: Literal["cannjudge_remote"] = "cannjudge_remote"
    submit_url: str
    requires_auth: bool = True
    score_source: Literal["remote_hidden_tests"] = "remote_hidden_tests"


class ObservedStats(StrictModel):
    """同步时观测到的公开通过统计，仅用作难度代理。"""

    pass_user_count: int = Field(ge=0)
    attempt_count: int = Field(ge=0)
    pass_rate: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_rate(self) -> Self:
        if self.attempt_count == 0 and self.pass_rate is not None:
            raise ValueError("attempt_count 为 0 时 pass_rate 必须为空")
        if self.attempt_count > 0:
            expected = self.pass_user_count / self.attempt_count
            if self.pass_rate is None or abs(self.pass_rate - expected) > 1e-12:
                raise ValueError("pass_rate 必须等于 pass_user_count / attempt_count")
        return self


class OperatorBenchmarkCase(StrictModel):
    """一条可交给代码 Agent 的算子工程任务。"""

    schema_version: Literal["operator-development.v1"] = "operator-development.v1"
    case_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    task_type: Literal["operator_implementation"] = "operator_implementation"
    prompt: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    product: Literal["CANN"] = "CANN"
    api_family: Literal["AscendC"] = "AscendC"
    version: str = Field(min_length=1)
    hardware: str = Field(min_length=1)
    project_template: str = Field(min_length=1)
    tags: list[str]
    source_docs: list[str] = Field(min_length=1)
    source: SourceRef
    judge: JudgeSpec
    observed_stats: ObservedStats

    @model_validator(mode="after")
    def validate_version_first_namespace(self) -> Self:
        expected = f"Huawei.CANN.AscendC.{self.version}"
        if self.namespace != expected:
            raise ValueError(f"namespace 必须为 {expected}")
        return self
