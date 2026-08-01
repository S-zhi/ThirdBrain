"""fix_signatures.py — 兜底补全 YAML 中空的 function signature。

策略：
  1. 扫 yaml/ 下所有 yaml
  2. 找 documents[].use.function_details.signature.value == "" 的项
  3. 从对应的 markdown 原文（API参考/{rel_path}.md）兜底提取
  4. 用 PyYAML 安全 dump 回去
  5. 写 tmp 报告

签名兜底：复用 markdown_yaml_v21.SIGNATURE_PATTERN + 跨行反斜杠拼接
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

import yaml

# 从 markdown_yaml_v21 复用签名正则（保持一致）
try:
    from src.script.markdown_yaml_v21 import SIGNATURE_PATTERN  # type: ignore
except Exception:
    SIGNATURE_PATTERN = re.compile(
        r"(?m)^[ \t]*(?:template\s*<[^>]+>\s*)?"
        r"(?:[\w:<>,*&\[\]\s]+\s+)?[A-Za-z_]\w*\s*\([^;{}]*\)\s*;?[ \t]*$"
    )


FENCE_PATTERN = re.compile(r"^\s*```")
CONT_SPLIT = re.compile(r"\\\s*\n\s*")


def extract_signatures_from_markdown(md_text: str, api_name: str) -> list[str]:
    """从 markdown 找所有可能的签名候选（含跨行续行）。"""
    candidates: list[str] = []
    in_code_block = False
    cur_block_lines: list[str] = []
    cur_block_heading = ""

    def flush() -> None:
        nonlocal cur_block_heading
        if re.search(r"(?:示例|example)", cur_block_heading, re.IGNORECASE):
            return
        block = "\n".join(cur_block_lines)
        # 正常提取
        for sig_m in SIGNATURE_PATTERN.finditer(block):
            sig = sig_m.group(0).strip()
            if not api_name or api_name in sig:
                candidates.append(sig)
        # 跨行拼接兜底
        joined = CONT_SPLIT.sub("", block)
        for sig_m in SIGNATURE_PATTERN.finditer(joined):
            sig = sig_m.group(0).strip()
            if not api_name or api_name in sig:
                if sig not in candidates:
                    candidates.append(sig)

    lines = md_text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if FENCE_PATTERN.match(line) and not in_code_block:
            # 找前 5 行的 heading
            for j in range(i - 1, max(i - 5, -1), -1):
                h = lines[j].strip()
                if h.startswith("#"):
                    cur_block_heading = h
                    break
                if h:
                    break
            in_code_block = True
            cur_block_lines = []
        elif FENCE_PATTERN.match(line) and in_code_block:
            in_code_block = False
            flush()
            cur_block_lines = []
            cur_block_heading = ""
        elif in_code_block:
            cur_block_lines.append(line)
        i += 1

    return candidates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yaml-dir", type=Path, default=Path("yaml"))
    parser.add_argument("--md-dir", type=Path, default=Path("API参考"))
    parser.add_argument("--report", type=Path, default=Path("data/pipeline/c_report.txt"))
    args = parser.parse_args()

    yaml_dir: Path = args.yaml_dir
    md_dir: Path = args.md_dir

    if not yaml_dir.is_dir():
        print(f"❌ yaml 目录不存在: {yaml_dir}", file=sys.stderr)
        return 1
    if not md_dir.is_dir():
        print(f"❌ md 目录不存在: {md_dir}", file=sys.stderr)
        return 1

    started = time.time()
    scanned = 0
    empty_before = 0
    fixed = 0
    remaining_empty = 0
    errors: list[str] = []

    yaml_files = sorted(yaml_dir.rglob("*.yaml"))
    print(f"扫描 {len(yaml_files)} 个 yaml 文件...", file=sys.stderr)

    for yaml_path in yaml_files:
        rel = yaml_path.relative_to(yaml_dir)
        md_path = md_dir / rel.with_suffix(".md")
        if not md_path.is_file():
            continue
        scanned += 1
        try:
            doc = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            errors.append(f"yaml parse {yaml_path}: {e}")
            continue
        if not isinstance(doc, dict) or "documents" not in doc:
            continue

        changed = False
        for d in doc["documents"]:
            if not isinstance(d, dict):
                continue
            use = d.get("use", {})
            if not isinstance(use, dict):
                continue
            fd = use.get("function_details", {})
            if not isinstance(fd, dict):
                continue
            sig = fd.get("signature")
            if not isinstance(sig, dict):
                continue
            if sig.get("value"):
                continue
            empty_before += 1
            api_name = d.get("name", "")
            try:
                md_text = md_path.read_text(encoding="utf-8")
            except OSError as e:
                errors.append(f"read md {md_path}: {e}")
                continue
            candidates = extract_signatures_from_markdown(md_text, api_name)
            if candidates:
                sig["value"] = candidates[0]
                sig["is_ai"] = False
                changed = True
                fixed += 1
            else:
                remaining_empty += 1

        if changed:
            try:
                yaml_path.write_text(
                    yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=100),
                    encoding="utf-8",
                )
            except OSError as e:
                errors.append(f"write yaml {yaml_path}: {e}")

    elapsed = time.time() - started
    lines = [
        f"scanned: {scanned}",
        f"empty_before: {empty_before}",
        f"fixed: {fixed}",
        f"remaining_empty: {remaining_empty}",
        f"errors: {len(errors)}",
        f"elapsed: {elapsed:.1f}s",
    ]
    for ln in lines:
        print(ln)
    for e in errors[:30]:
        print(f"  - {e}")

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
