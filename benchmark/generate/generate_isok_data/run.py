#!/usr/bin/env python3
"""
CLI 入口：benchmark 评测数据生成工具。

用法：
    python run.py --docs_dir ./md --count 3 --output output.jsonl
    python run.py --docs_dir ./my_api_docs --count 10 --output benchmark.jsonl --workers 5
    python run.py --docs_dir ./md --count 2 --mock  # 跳过 LLM，测试流程
"""

import argparse
import sys
from pathlib import Path

# 将项目根目录加入 sys.path，确保模块间导入正确
PROJECT_DIR = Path(__file__).parent.resolve()
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from pipeline import run_pipeline

from config import (
    DEFAULT_DOCS_DIR,
    DEFAULT_OUTPUT,
    DEFAULT_QUESTION_COUNT,
    LLM_BASE_URL,
    LLM_MODEL_NAME,
)


def parse_args() -> argparse.Namespace:
    """解析数据生成命令行参数。"""
    parser = argparse.ArgumentParser(
        description="从 API 文档自动生成 benchmark 评测数据",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python run.py --docs_dir ./md --count 3
  python run.py --docs_dir ./my_docs --count 10 --output benchmark.jsonl
  python run.py --docs_dir ./my_docs --count 10 --workers 5
  python run.py --docs_dir ./md --count 2 --mock
        """,
    )
    parser.add_argument(
        "--docs_dir",
        type=str,
        default=str(DEFAULT_DOCS_DIR),
        help=f"API 文档目录路径（默认: {DEFAULT_DOCS_DIR}）",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=DEFAULT_QUESTION_COUNT,
        help=f"要生成的问题总数（默认: {DEFAULT_QUESTION_COUNT}）",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=str(DEFAULT_OUTPUT),
        help=f"输出 JSONL 文件路径（默认: {DEFAULT_OUTPUT}）",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="使用 mock 模式（跳过 LLM 调用，用于测试流程逻辑）",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="覆盖输出文件而非追加",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="并行 worker 数量（默认: 1，即串行执行。建议设置为 3-10 以加速）",
    )
    return parser.parse_args()


def main() -> None:
    """校验参数并执行 benchmark 数据生成流程。"""
    args = parse_args()

    # 验证参数
    docs_dir = Path(args.docs_dir).resolve()
    if not docs_dir.is_dir():
        print(f"错误: 文档目录不存在: {docs_dir}")
        sys.exit(1)

    if args.count < 1:
        print("错误: --count 必须 >= 1")
        sys.exit(1)

    if args.workers < 1:
        print("错误: --workers 必须 >= 1")
        sys.exit(1)

    output_path = Path(args.output).resolve()

    # 如果需要覆盖输出文件，先清空
    if args.clear and output_path.exists():
        output_path.unlink()
        print(f"已清空输出文件: {output_path}")

    print(f"文档目录: {docs_dir}")
    print(f"问题数量: {args.count}")
    print(f"并行 workers: {args.workers}")
    print(f"输出路径: {output_path}")
    print(f"运行模式: {'MOCK' if args.mock else 'LLM'}")
    if not args.mock:
        print(f"MiniMax 模型: {LLM_MODEL_NAME}")
        print(f"MiniMax 接口: {LLM_BASE_URL}")
    print()

    try:
        records = run_pipeline(
            docs_dir=docs_dir,
            question_count=args.count,
            output_path=output_path,
            mock_mode=args.mock,
            workers=args.workers,
        )

        # 打印摘要
        print("\n" + "=" * 60)
        print("生成结果摘要")
        print("=" * 60)
        print(f"总记录数: {len(records)}")

        # 标签分布
        tag_counts = {}
        for r in records:
            tag_counts[r.tag] = tag_counts.get(r.tag, 0) + 1
        print("标签分布:")
        for tag, count in sorted(tag_counts.items()):
            print(f"  {tag}: {count}")

        # 选取模式分布
        mode_counts = {}
        for r in records:
            mode_counts[r.selection_mode] = mode_counts.get(r.selection_mode, 0) + 1
        print("选取模式分布:")
        for mode, count in sorted(mode_counts.items()):
            print(f"  {mode}: {count}")

    except FileNotFoundError as e:
        print(f"\n错误: {e}")
        sys.exit(1)
    except Exception as e:  # noqa: BLE001 - CLI 边界统一打印未预期错误
        print(f"\n未预期的错误: {type(e).__name__}: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
