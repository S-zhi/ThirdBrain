#!/usr/bin/env python3
"""
Markdown Minimal-Unit Filter
=============================

递归扫描一个文档目录，过滤掉目录（TOC）/ 索引 / 概览 / 简介 / 预留接口等
"非最小单元"内容，保留单 API / 单函数定义类的 Markdown 文件，把它们的
绝对路径逐行写入一个 text 文件。

核心策略（四级判断）：

    1. 文件名后缀（无需读正文）
       例：*列表.md / *概览.md / *概述.md / *简介.md / *预留接口.md
    2. H1 标题（精确匹配，概念页 / 预留接口页）
       例：# 简介 / # 概览 / # 预留接口
    3. 正文 listing 话术（兜底）
       例："本章节列出..."
    4. 正文结构 —— 双信号：
         a) H4 + API spec 标记（正向） → INCLUDE
            例：#### 功能说明 / #### 函数原型 / #### 参数说明
         b) 否则，链接密度 > 30% 视为 breadcrumb/nav（兜底） → EXCLUDE
            真正的"有内容但 degraded"的 API 文件链接密度通常 < 20%

用法：
    python3 filter_md.py --src <dir> --out <text-file> [--verbose] [--dry-run]
                          [--exclude-pattern <regex>] [--exclude-h1 <text>]

输出：
    --out 指定的文件：每行一个绝对路径
    --out 同目录下的 <out>.excluded.txt：被剔除文件清单（路径 \\t 原因）
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


# ---------------------------------------------------------------------------
# 1. 路径/扩展名黑名单
# ---------------------------------------------------------------------------

JUNK_DIRS: frozenset[str] = frozenset({
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__",
    ".idea", ".ruff_cache", ".pytest_cache", ".mypy_cache",
    "dist", "build", ".next", ".cache",
})

ALLOWED_EXTS: frozenset[str] = frozenset({".md", ".markdown"})

# 链接密度阈值：超过此值且无 H4 API spec → 视为 breadcrumb/nav 排除
# 实测：本仓库 290 行纯 nav 页链接密度稳定在 31.7%；含真内容的页 < 20%
LINK_DENSITY_THRESHOLD: float = 0.30


def _has_junk_ancestor(p: Path) -> bool:
    return any(part in JUNK_DIRS for part in p.parts)


def walk_candidates(src: Path) -> list[Path]:
    """递归产出 src 下所有 .md / .markdown 文件，跳过垃圾目录。"""
    out: list[Path] = []
    for p in src.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in ALLOWED_EXTS:
            continue
        if _has_junk_ancestor(p):
            continue
        out.append(p)
    return out


# ---------------------------------------------------------------------------
# 2. 剔除规则定义
# ---------------------------------------------------------------------------

# 文件名 basename（无 .md）完整等于以下任一 → 视为 TOC / 概览
FILENAME_EXACT_EXCLUDE: frozenset[str] = frozenset({
    "index", "readme", "summary", "overview", "toc",
    "allapi", "all_api", "all-api", "all apis", "all_apis",
})

# 文件名 basename（无 .md）以这些后缀结尾 → 视为 TOC / 概览
# 必须带 $ 锚定，避免误伤 DataTypeList、ListTensorDesc、GetIndex 等真 API
FILENAME_SUFFIX_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"列表$"),
    re.compile(r"一览$"),
    re.compile(r"概览$"),
    re.compile(r"概述$"),
    re.compile(r"总览$"),
    re.compile(r"导航$"),
    re.compile(r"目录$"),
    re.compile(r"简介$"),
    re.compile(r"前言$"),
    re.compile(r"预留接口$"),
    re.compile(r"(?i)readme$"),
    re.compile(r"(?i)summary$"),
    re.compile(r"(?i)overview$"),
    re.compile(r"(?i)toc$"),
)

# H1（首个 # 标题）文本精确等于以下任一 → 视为概念页 / 预留接口页
H1_EXACT_EXCLUDE: frozenset[str] = frozenset({
    # 中文
    "简介", "概览", "概述", "总览", "前言",
    "目录", "索引", "汇总", "导航",
    "预留接口", "废弃接口",
    # 英文
    "Introduction", "Overview", "Preface", "Foreword", "About",
    "Reserved", "Reserved Interfaces", "Deprecated",
    "Index", "Readme", "Summary", "TOC", "Contents",
})

# 正文中的"列出型"话术 → 进一步兜底
BODY_LISTING_HINTS: tuple[re.Pattern[str], ...] = (
    re.compile(r"本章节列出"),
    re.compile(r"本章列出"),
    re.compile(r"本章\s*罗列"),
    re.compile(r"以下为(?:本)?(?:模块|章节)?(?:全部|所有)?(?:接口|API|算子|函数)"),
    re.compile(r"本节(?:仅)?列出"),
)

# 判定"最小单元"的 positive signal：H4 标题里出现以下任一关键词
API_SPEC_MARKERS: tuple[str, ...] = (
    # 核心规格段
    "功能说明", "函数原型", "参数说明", "原型定义", "函数说明",
    "返回值说明", "约束说明", "调用示例", "注意事项",
    # 数据/类型段
    "需要包含的头文件", "Public成员函数", "模板参数", "参数类型",
    "成员函数", "字段说明", "属性说明",
    # 英文变体（兼容英文文档）
    "Function Prototype", "Parameters", "Returns", "Description",
)

# 提取 H1 / H4 标题的正则
H1_RE: re.Pattern[str] = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
H4_RE: re.Pattern[str] = re.compile(r"^####\s+(.+?)\s*$", re.MULTILINE)

# 识别 markdown 链接（行内或引用形式）→ 用于 link density 统计
LINK_LINE_RE: re.Pattern[str] = re.compile(r"\[[^\]]+\]\([^)]+\)|^\s*\[[^\]]+\]:\s+")


# ---------------------------------------------------------------------------
# 核心判断
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Decision:
    path: Path
    included: bool
    reason: str  # 人类可读的判断原因


def _classify_filename(
    base: str,
    extra_filename_patterns: Sequence[re.Pattern[str]],
) -> str | None:
    """命中剔除规则时返回 reason；否则 None。"""
    if base.lower() in FILENAME_EXACT_EXCLUDE:
        return f"filename_exact: {base!r}"
    for pat in FILENAME_SUFFIX_PATTERNS:
        if pat.search(base):
            return f"filename_suffix: {base!r} matches {pat.pattern!r}"
    for pat in extra_filename_patterns:
        if pat.search(base):
            return f"filename(custom): {base!r} matches {pat.pattern!r}"
    return None


def _link_density(text: str) -> tuple[float, int]:
    """返回 (link_density, total_lines)。"""
    lines = text.splitlines()
    n = len(lines)
    if n == 0:
        return 0.0, 0
    link_lines = sum(1 for l in lines if LINK_LINE_RE.search(l))
    return link_lines / n, n


def classify(
    p: Path,
    extra_filename_patterns: Sequence[re.Pattern[str]] = (),
    extra_h1_exact: Sequence[str] = (),
) -> Decision:
    """判断一个 .md 文件是否属于"最小单元"。

    决策顺序（短路求值）：
        filename → h1_exact → body_hint → has_api_spec → link_density → INCLUDE/EXCLUDE
    """
    base = p.stem  # 文件名去掉扩展名

    # 1. 文件名检查（无需读正文）
    fname_reason = _classify_filename(base, extra_filename_patterns)
    if fname_reason is not None:
        return Decision(p, False, fname_reason)

    # 2. 读正文
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return Decision(p, False, f"read_error: {e}")

    # 3. H1 标题（精确）
    m = H1_RE.search(text)
    if m:
        h1 = m.group(1).strip()
        if h1 in H1_EXACT_EXCLUDE or h1 in extra_h1_exact:
            return Decision(p, False, f"h1_exact: {h1!r}")

    # 4. 正文 listing 兜底
    for hint in BODY_LISTING_HINTS:
        if hint.search(text):
            return Decision(p, False, f"body_hint: {hint.pattern!r}")

    # 5. H4 + API spec 检查（正向信号）
    h4_sections: list[str] = H4_RE.findall(text)
    has_api_spec = any(
        any(marker in h4 for marker in API_SPEC_MARKERS)
        for h4 in h4_sections
    )
    if has_api_spec:
        return Decision(
            p, True,
            f"passed: {len(h4_sections)} H4 sections, has API spec markers"
        )

    # 6. 没有 H4 → 用 link density 兜底（区分"真 degraded API" vs "纯 nav"）
    density, n_lines = _link_density(text)
    if n_lines == 0:
        return Decision(p, False, "empty file")

    if density > LINK_DENSITY_THRESHOLD:
        return Decision(
            p, False,
            f"breadcrumb: {density*100:.0f}% link density > {LINK_DENSITY_THRESHOLD*100:.0f}%, no H4 API spec"
        )

    # 通过所有检查
    return Decision(
        p, True,
        f"passed (no H4, {density*100:.0f}% link density, {n_lines} lines)"
    )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="过滤一个文档树，挑出适合进 RAG 知识库的 Markdown 最小单元。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--src", required=True, type=Path, help="要扫描的根目录（递归）。")
    p.add_argument(
        "--out", "-o", required=True, type=Path,
        help="输出 text 文件路径，每行一个绝对路径。",
    )
    p.add_argument(
        "--exclude-pattern", action="append", default=[], metavar="REGEX",
        help="额外的文件名剔除正则（可重复，命中即剔除）。",
    )
    p.add_argument(
        "--exclude-h1", action="append", default=[], metavar="TEXT",
        help="额外的 H1 精确剔除文本（可重复）。",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="只盘点不写文件，先看汇总再决定。",
    )
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="打印每个被剔除的文件 + 原因。",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_argparser()
    args = parser.parse_args(argv)

    src: Path = args.src.resolve()
    out: Path = args.out

    if not src.is_dir():
        print(f"ERROR: --src 不是目录或不存在: {src}", file=sys.stderr)
        return 2

    extra_filename = tuple(re.compile(pat) for pat in args.exclude_pattern)
    extra_h1 = tuple(args.exclude_h1)

    decisions: list[Decision] = []
    for p in sorted(walk_candidates(src)):
        decisions.append(classify(p, extra_filename, extra_h1))

    included = [d for d in decisions if d.included]
    excluded = [d for d in decisions if not d.included]

    # verbose 模式打印每个决策
    if args.verbose:
        for d in decisions:
            tag = "INCL" if d.included else "EXCL"
            print(f"  [{tag}] {d.reason:<70}  {d.path}")

    # 写主输出（除非 dry-run）
    if not args.dry_run:
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8", newline="\n") as f:
                for d in included:
                    f.write(str(d.path.resolve()) + "\n")
            tmp.replace(out)  # 原子 rename
        except OSError as e:
            print(f"ERROR: 写入 {out} 失败: {e}", file=sys.stderr)
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
            return 3

        # 写 excluded 报告（同目录 + .excluded.txt）
        excluded_path = out.with_name(out.name + ".excluded.txt")
        try:
            with excluded_path.open("w", encoding="utf-8", newline="\n") as f:
                for d in excluded:
                    f.write(f"{d.path.resolve()}\t{d.reason}\n")
        except OSError as e:
            print(f"WARN: 写 excluded 报告失败: {e}", file=sys.stderr)

    # 汇总
    total = len(decisions)
    print(f"\n[md-minimal-unit-filter] 扫描完成")
    print(f"  src:                 {src}")
    print(f"  out:                 {out if not args.dry_run else '(dry-run, 未写入)'}")
    print(f"  total .md files:     {total}")
    print(f"  INCLUDED (min unit): {len(included)}")
    print(f"  EXCLUDED (TOC/...):  {len(excluded)}")
    if not args.dry_run:
        print(f"\n  ✓ 已写入 {out}（{len(included)} 个路径）")
        if excluded:
            print(f"  ✓ 已写入 {out}.excluded.txt（{len(excluded)} 个剔除项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
