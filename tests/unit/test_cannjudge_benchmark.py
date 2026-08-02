import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from benchmark.cannjudge.client import CannJudgeError
from benchmark.cannjudge.models import ObservedStats
from benchmark.cannjudge.sync import (
    build_case,
    fetch_cases,
    render_source_doc,
    select_contests,
    write_jsonl,
    write_source_docs,
)


def _contest() -> dict[str, Any]:
    return {
        "_id": "contest-s1",
        "name": "s1",
        "title": "算子挑战赛 S1",
    }


def _problem() -> dict[str, Any]:
    return {
        "_id": "problem-addcmul",
        "name": "addcmul",
        "title": "Addcmul",
        "desc": "实现 y = input_data + x1 * x2 * value，并支持广播和非对齐场景。",
        "cann_version": "8.5.0",
        "code_template": "custom_template",
        "version_no": 2,
        "tags": ["vector", "s1"],
    }


def _case():
    return build_case(
        base_url="https://cannjudge.example",
        group_id="public-group",
        contest=_contest(),
        problem=_problem(),
        stats=ObservedStats(pass_user_count=3, attempt_count=12, pass_rate=0.25),
    )


def test_build_case_is_version_first_and_traceable() -> None:
    case = _case()

    assert case.case_id == "cannjudge.s1.addcmul.v2"
    assert case.namespace == "Huawei.CANN.AscendC.8.5.0"
    assert case.hardware == "910B"
    assert case.project_template == "自定义算子工程"
    assert case.source.problem_url == "https://cannjudge.example/public/s1/addcmul"
    assert case.judge.requires_auth is True
    assert case.observed_stats.pass_rate == 0.25
    assert "支持广播和非对齐场景" in case.prompt


def test_missing_cann_version_is_rejected() -> None:
    problem = _problem()
    problem.pop("cann_version")

    with pytest.raises(CannJudgeError, match="cann_version"):
        build_case(
            base_url="https://cannjudge.example",
            group_id="public-group",
            contest=_contest(),
            problem=problem,
            stats=ObservedStats(pass_user_count=0, attempt_count=0),
        )


def test_observed_stats_rejects_inconsistent_rate() -> None:
    with pytest.raises(ValidationError, match="pass_rate"):
        ObservedStats(pass_user_count=1, attempt_count=2, pass_rate=0.75)


def test_select_contests_matches_name_title_and_id() -> None:
    contests = [
        _contest(),
        {"_id": "contest-s2", "name": "s2", "title": "算子挑战赛 S2"},
    ]

    selected = select_contests(contests, ["算子挑战赛 S2", "contest-s1"])

    assert [item["name"] for item in selected] == ["s2", "s1"]


class FakeSource:
    base_url = "https://cannjudge.example"

    def public_group(self) -> dict[str, Any]:
        return {"_id": "public-group", "name": "public"}

    def contests(self, group_id: str) -> list[dict[str, Any]]:
        assert group_id == "public-group"
        return [_contest()]

    def problems(self, contest_id: str) -> list[dict[str, Any]]:
        assert contest_id == "contest-s1"
        return [_problem()]

    def problem_stats(self, contest_id: str) -> list[dict[str, Any]]:
        assert contest_id == "contest-s1"
        return [
            {
                "problem_id": "problem-addcmul",
                "passUserCount": 5,
                "attemptCount": 20,
            }
        ]


def test_fetch_cases_joins_problem_stats() -> None:
    cases = fetch_cases(FakeSource(), ["s1"])

    assert len(cases) == 1
    assert cases[0].observed_stats.pass_user_count == 5
    assert cases[0].observed_stats.pass_rate == 0.25


def test_writers_produce_jsonl_and_generator_compatible_markdown(tmp_path: Path) -> None:
    case = _case()
    output = tmp_path / "cases.jsonl"
    docs_dir = tmp_path / "docs"

    write_jsonl([case], output)
    write_source_docs([case], docs_dir)

    row = json.loads(output.read_text(encoding="utf-8"))
    assert row["case_id"] == case.case_id
    assert row["namespace"] == "Huawei.CANN.AscendC.8.5.0"

    doc_path = docs_dir / case.source_docs[0]
    markdown = doc_path.read_text(encoding="utf-8")
    assert markdown == render_source_doc(case)
    assert "## 算子开发任务" in markdown
    assert 'namespace: "Huawei.CANN.AscendC.8.5.0"' in markdown
