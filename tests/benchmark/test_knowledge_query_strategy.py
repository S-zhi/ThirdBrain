"""Knowledge Query V1 golden case 的 schema 与覆盖面回归。"""

from __future__ import annotations

import json
from pathlib import Path


def test_knowledge_query_v1_cases_cover_core_regressions() -> None:
    """Benchmark 必须持续覆盖隔离、异步加工和预算来源。"""
    path = Path(__file__).parent / "cases" / "knowledge_query_v1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["schema_version"] == "1.0"
    cases = {case["id"]: case for case in payload["cases"]}
    assert "official-namespace-case-isolation" in cases
    assert cases["micro-budget-provenance"]["require_provenance"] is True
    assert cases["micro-budget-provenance"]["max_capsule_items"] == 3
    assert cases["source-hit-without-artifact"]["expected_enrichment"]
