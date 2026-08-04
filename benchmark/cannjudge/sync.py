"""把 CANN Judge 公开题库同步成算子开发 benchmark。"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from benchmark.cannjudge.client import CannJudgeClient, CannJudgeError
from benchmark.cannjudge.models import (
    JudgeSpec,
    ObservedStats,
    OperatorBenchmarkCase,
    SourceRef,
)

DEFAULT_BASE_URL = "https://cannjudge.cn"
DEFAULT_CONTESTS = ("s1", "s2")
GENERATED_DIR = Path(__file__).resolve().parent / "generated"
DEFAULT_OUTPUT = GENERATED_DIR / "operator_scenarios.jsonl"
DEFAULT_DOCS_DIR = GENERATED_DIR / "docs"

TEMPLATE_LABELS = {
    "custom_template": "自定义算子工程",
    "gitcode_template_noopapi": "开源仓算子工程（无 opapi）",
    "gitcode_template_opapi": "开源仓算子工程（可编辑 opapi）",
    "hccl": "HCCL 算子工程",
}


class CannJudgeDataSource(Protocol):
    """便于用离线 fixture 验证同步流程的数据源协议。"""

    base_url: str

    def public_group(self) -> dict[str, Any]: ...

    def contests(self, group_id: str) -> list[dict[str, Any]]: ...

    def problems(self, contest_id: str) -> list[dict[str, Any]]: ...

    def problem_stats(self, contest_id: str) -> list[dict[str, Any]]: ...


def _required_text(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = str(data.get(key) or "").strip()
        if value:
            return value
    raise CannJudgeError(f"缺少必填字段: {' / '.join(keys)}")


def _safe_token(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-.")
    if not token:
        raise CannJudgeError(f"无法生成安全标识符: {value!r}")
    return token.lower()


def _stats_for_problem(
    problem_id: str,
    stats_by_problem: dict[str, dict[str, Any]],
) -> ObservedStats:
    raw = stats_by_problem.get(problem_id, {})
    pass_count = max(0, int(raw.get("passUserCount") or raw.get("passCount") or 0))
    attempt_count = max(0, int(raw.get("attemptCount") or 0))
    pass_rate = pass_count / attempt_count if attempt_count else None
    return ObservedStats(
        pass_user_count=pass_count,
        attempt_count=attempt_count,
        pass_rate=pass_rate,
    )


def build_case(
    *,
    base_url: str,
    group_id: str,
    contest: dict[str, Any],
    problem: dict[str, Any],
    stats: ObservedStats,
) -> OperatorBenchmarkCase:
    """把 CANN Judge 原始对象规范化为 version-first 场景。"""
    contest_id = _required_text(contest, "_id", "id")
    contest_name = _required_text(contest, "name")
    problem_id = _required_text(problem, "_id", "id")
    problem_name = _required_text(problem, "name")
    title = _required_text(problem, "title", "name")
    version = _required_text(problem, "cann_version", "cannVersion")
    description = _required_text(problem, "desc", "description")
    version_no = max(1, int(problem.get("version_no") or problem.get("versionNo") or 1))

    template_value = str(
        problem.get("code_template")
        or problem.get("codeTemplate")
        or problem.get("template_type")
        or "custom_template"
    ).strip()
    hardware = str(
        problem.get("chip_type")
        or problem.get("chipType")
        or ("950-cpu" if template_value == "hccl" else "910B")
    ).strip()
    project_template = TEMPLATE_LABELS.get(template_value, template_value)
    tags = [str(tag).strip() for tag in problem.get("tags") or [] if str(tag).strip()]

    contest_token = _safe_token(contest_name)
    problem_token = _safe_token(problem_name)
    case_id = f"cannjudge.{contest_token}.{problem_token}.v{version_no}"
    source_doc = f"{case_id}.md"
    site_root = base_url.rstrip("/")
    problem_url = f"{site_root}/public/{contest_token}/{problem_token}"
    submit_url = f"{problem_url}/submit"
    prompt = (
        f"请在 {hardware} 上使用 Ascend C / CANN {version} 完成 {title} 算子工程，"
        "提交物需通过 CANN Judge 隐藏测试。\n\n"
        f"{description.strip()}"
    )

    return OperatorBenchmarkCase(
        case_id=case_id,
        title=title,
        prompt=prompt,
        namespace=f"Huawei.CANN.AscendC.{version}",
        version=version,
        hardware=hardware,
        project_template=project_template,
        tags=tags,
        source_docs=[source_doc],
        source=SourceRef(
            base_url=site_root,
            group_id=group_id,
            contest_id=contest_id,
            contest_name=contest_name,
            problem_id=problem_id,
            problem_name=problem_name,
            problem_url=problem_url,
        ),
        judge=JudgeSpec(submit_url=submit_url),
        observed_stats=stats,
    )


def select_contests(
    contests: Sequence[dict[str, Any]],
    selectors: Sequence[str],
) -> list[dict[str, Any]]:
    """按 id、name 或 title 精确选择赛事，并保持用户指定顺序。"""
    selected: list[dict[str, Any]] = []
    for selector in selectors:
        normalized = selector.strip().casefold()
        match = next(
            (
                contest
                for contest in contests
                if normalized
                in {
                    str(contest.get("_id") or "").strip().casefold(),
                    str(contest.get("id") or "").strip().casefold(),
                    str(contest.get("name") or "").strip().casefold(),
                    str(contest.get("title") or "").strip().casefold(),
                }
            ),
            None,
        )
        if match is None:
            available = ", ".join(
                str(item.get("name") or item.get("title") or item.get("_id")) for item in contests
            )
            raise CannJudgeError(f"未找到赛事 {selector!r}；可选值: {available}")
        if match not in selected:
            selected.append(match)
    return selected


def fetch_cases(
    source: CannJudgeDataSource,
    contest_selectors: Sequence[str] = DEFAULT_CONTESTS,
) -> list[OperatorBenchmarkCase]:
    """从公开 API 拉取并构建场景，不访问登录态接口。"""
    group = source.public_group()
    group_id = _required_text(group, "_id", "id")
    contests = select_contests(source.contests(group_id), contest_selectors)
    cases: list[OperatorBenchmarkCase] = []

    for contest in contests:
        contest_id = _required_text(contest, "_id", "id")
        raw_stats = source.problem_stats(contest_id)
        stats_by_problem = {
            str(item.get("problem_id") or item.get("_id") or "").strip(): item
            for item in raw_stats
            if str(item.get("problem_id") or item.get("_id") or "").strip()
        }
        problems = sorted(
            source.problems(contest_id),
            key=lambda item: (
                str(item.get("title") or item.get("name") or "").casefold(),
                str(item.get("_id") or item.get("id") or ""),
            ),
        )
        for problem in problems:
            problem_id = _required_text(problem, "_id", "id")
            cases.append(
                build_case(
                    base_url=source.base_url,
                    group_id=group_id,
                    contest=contest,
                    problem=problem,
                    stats=_stats_for_problem(problem_id, stats_by_problem),
                )
            )

    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise CannJudgeError("同步结果包含重复 case_id，请检查赛事或题目 slug")
    return cases


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(content)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def write_jsonl(cases: Sequence[OperatorBenchmarkCase], output_path: Path) -> None:
    """原子写入 JSONL，重复同步不会产生追加重复。"""
    content = "".join(f"{case.model_dump_json()}\n" for case in cases)
    _atomic_write(output_path, content)


def _yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_source_doc(case: OperatorBenchmarkCase) -> str:
    """生成可直接交给现有 generate_isok_data 的 Markdown 文档。"""
    stats = case.observed_stats
    pass_rate = f"{stats.pass_rate:.2%}" if stats.pass_rate is not None else "暂无提交"
    tags = ", ".join(case.tags) or "无"
    return (
        "---\n"
        f"schema_version: {_yaml_string(case.schema_version)}\n"
        f"case_id: {_yaml_string(case.case_id)}\n"
        f"namespace: {_yaml_string(case.namespace)}\n"
        f"hardware: {_yaml_string(case.hardware)}\n"
        f"project_template: {_yaml_string(case.project_template)}\n"
        f"source_url: {_yaml_string(case.source.problem_url)}\n"
        "---\n\n"
        f"# {case.title}\n\n"
        f"- CANN 版本：`{case.version}`\n"
        f"- 芯片：`{case.hardware}`\n"
        f"- 工程模板：{case.project_template}\n"
        f"- 标签：{tags}\n"
        f"- 公开通过率：{pass_rate}（{stats.pass_user_count}/{stats.attempt_count}）\n"
        f"- 在线题面：{case.source.problem_url}\n"
        f"- 在线提交：{case.judge.submit_url}（需要登录）\n\n"
        "## 算子开发任务\n\n"
        f"{case.prompt}\n"
    )


def write_source_docs(cases: Sequence[OperatorBenchmarkCase], docs_dir: Path) -> None:
    """逐题输出 Markdown；不删除目录内的其他文件。"""
    docs_dir.mkdir(parents=True, exist_ok=True)
    for case in cases:
        _atomic_write(docs_dir / case.source_docs[0], render_source_doc(case))


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="同步 CANN Judge 公开题库，生成算子开发 benchmark 场景和 Markdown 语料",
    )
    parser.add_argument(
        "--contest",
        action="append",
        dest="contests",
        help="赛事 id/name/title，可重复传入；默认同步 s1 和 s2",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="CANN Judge 站点地址")
    parser.add_argument("--timeout", type=float, default=30.0, help="单次 HTTP 请求超时秒数")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="场景 JSONL 输出路径")
    parser.add_argument(
        "--docs-dir",
        type=Path,
        default=DEFAULT_DOCS_DIR,
        help="兼容现有 benchmark 生成器的 Markdown 输出目录",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    selectors = tuple(args.contests or DEFAULT_CONTESTS)
    try:
        with CannJudgeClient(args.base_url, timeout=args.timeout) as client:
            cases = fetch_cases(client, selectors)
        write_jsonl(cases, args.output)
        write_source_docs(cases, args.docs_dir)
    except CannJudgeError as exc:
        print(f"同步失败: {exc}")
        return 1

    namespaces = sorted({case.namespace for case in cases})
    print(f"已同步 {len(cases)} 个算子开发场景")
    print(f"赛事: {', '.join(selectors)}")
    print(f"命名空间: {', '.join(namespaces)}")
    print(f"场景 JSONL: {args.output.resolve()}")
    print(f"Markdown 语料: {args.docs_dir.resolve()}")
    print("在线提交需要 CANN Judge 登录态，当前同步器只访问公开只读 API。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
