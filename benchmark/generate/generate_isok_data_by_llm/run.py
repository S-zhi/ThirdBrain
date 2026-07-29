#!/usr/bin/env python3
"""CLI 入口：并发生成每条 benchmark case 的 model_output。"""

import argparse
import os
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
SOURCE_GENERATOR_DIR = PROJECT_DIR.parent / "generate_isok_data"
GENERATE_DIR = PROJECT_DIR.parent
if str(GENERATE_DIR) not in sys.path:
    sys.path.insert(0, str(GENERATE_DIR))

from generate_isok_data.config import LLM_BASE_URL, LLM_MODEL_NAME, llm_call

from generate_isok_data_by_llm.direct_flow import DirectAnswerFlow
from generate_isok_data_by_llm.flow_types import AnswerFlow
from generate_isok_data_by_llm.pipeline import (
    CODING_OUTPUT_MAX_CHARS,
    FINAL_OUTPUT_MAX_CHARS,
    SMALL_OUTPUT_MAX_CHARS,
    enrich_jsonl,
)
from generate_isok_data_by_llm.rag_flow import (
    RagIdAnswerFlow,
    RagIdDocumentStore,
    RagIdSettings,
)
from generate_isok_data_by_llm.retrieval_answer import RetrievalAnswerGenerator
from generate_isok_data_by_llm.web_flow import (
    TavilySearchClient,
    WebAnswerFlow,
    WebSettings,
)

DEFAULT_INPUT = SOURCE_GENERATOR_DIR / "output.jsonl"
DEFAULT_DIRECT_OUTPUT = PROJECT_DIR / "output_with_model_output.jsonl"
DEFAULT_WEB_OUTPUT = PROJECT_DIR / "output_with_web_model_output.jsonl"
DEFAULT_RAG_ID_OUTPUT = PROJECT_DIR / "output_with_rag_id_model_output.jsonl"
PROMPTS_DIR = PROJECT_DIR / "prompts"
DEFAULT_RAG_DOCUMENTS_DIR = SOURCE_GENERATOR_DIR / "md"
DEFAULT_TAVILY_API_URL = "https://api.tavily.com/search"


def parse_args() -> argparse.Namespace:
    """解析 JSONL 模型回答生成参数。"""
    parser = argparse.ArgumentParser(
        description="读取 benchmark JSONL，并发生成 model_output 到新的 JSONL",
    )
    parser.add_argument(
        "--mode",
        choices=("direct", "web", "rag_id"),
        default="direct",
        help="回答流程：direct、公开网络 web、内部精确检索 rag_id；默认 direct",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help=f"输入 JSONL，默认 {DEFAULT_INPUT}",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="新输出 JSONL；默认根据 mode 使用相互隔离的输出文件",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=10,
        help="模型请求并发数，默认 10",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="清空已有新输出并从头执行；不会删除或覆盖输入文件",
    )
    parser.add_argument(
        "--rag-documents-dir",
        type=Path,
        default=DEFAULT_RAG_DOCUMENTS_DIR,
        help=f"source_docs ID 对应的 RAG 语料目录，默认 {DEFAULT_RAG_DOCUMENTS_DIR}",
    )
    parser.add_argument(
        "--rag-context-max-chars",
        type=int,
        default=40_000,
        help="最终回答请求携带的 RAG 上下文字符上限，默认 40000",
    )
    parser.add_argument(
        "--web-api-url",
        default=DEFAULT_TAVILY_API_URL,
        help=f"Tavily Search API 地址，默认 {DEFAULT_TAVILY_API_URL}",
    )
    parser.add_argument(
        "--web-max-queries",
        type=int,
        default=3,
        help="MiniMax 最多规划的网络查询数，范围 1～20，默认 3",
    )
    parser.add_argument(
        "--web-max-results",
        type=int,
        default=3,
        help="每条网络查询返回的结果数，范围 1～20，默认 3",
    )
    parser.add_argument(
        "--web-context-max-chars",
        type=int,
        default=40_000,
        help="最终回答请求携带的网络资料字符上限，默认 40000",
    )
    parser.add_argument(
        "--web-timeout",
        type=float,
        default=30.0,
        help="单次 Tavily 请求超时秒数，默认 30",
    )
    return parser.parse_args()


def _build_answer_flow(args: argparse.Namespace) -> AnswerFlow:
    """根据 mode 构建 direct、web 或 rag_id 回答流程。"""
    if args.mode == "direct":
        return DirectAnswerFlow(llm_call, PROMPTS_DIR / "direct_answer.md")

    answer_generator = RetrievalAnswerGenerator(
        llm_call,
        PROMPTS_DIR / "retrieval_answer.md",
    )
    if args.mode == "rag_id":
        rag_settings = RagIdSettings(
            documents_dir=args.rag_documents_dir,
            context_max_chars=args.rag_context_max_chars,
        )
        return RagIdAnswerFlow(
            document_store=RagIdDocumentStore(rag_settings),
            answer_generator=answer_generator,
            settings=rag_settings,
        )

    web_settings = WebSettings(
        api_url=args.web_api_url,
        api_key=os.environ.get("TAVILY_API_KEY", ""),
        max_queries=args.web_max_queries,
        max_results=args.web_max_results,
        context_max_chars=args.web_context_max_chars,
        timeout=args.web_timeout,
    )
    return WebAnswerFlow(
        model_call=llm_call,
        search_client=TavilySearchClient(web_settings),
        answer_generator=answer_generator,
        settings=web_settings,
        planner_prompt_path=PROMPTS_DIR / "web_query_planner.md",
    )


def _default_output(mode: str) -> Path:
    """返回每种流程相互隔离的默认 JSONL 输出路径。"""
    if mode == "web":
        return DEFAULT_WEB_OUTPUT
    if mode == "rag_id":
        return DEFAULT_RAG_ID_OUTPUT
    return DEFAULT_DIRECT_OUTPUT


def main() -> int:
    """执行 JSONL 模型回答增强并返回进程退出码。"""
    args = parse_args()
    output_path = args.output or _default_output(args.mode)
    print(f"输入文件: {args.input.expanduser().resolve()}")
    print(f"输出文件: {output_path.expanduser().resolve()}")
    print(f"回答流程: {args.mode}")
    print(f"MiniMax 模型: {LLM_MODEL_NAME}")
    print(f"MiniMax 接口: {LLM_BASE_URL}")
    print(f"并发 workers: {args.workers}")
    print(
        f"输出限制: 普通 case 首次 ≤ {SMALL_OUTPUT_MAX_CHARS} 字，"
        f"达到上限后重跑 ≤ {CODING_OUTPUT_MAX_CHARS} 字，"
        f"再次达到上限后重跑 ≤ {FINAL_OUTPUT_MAX_CHARS} 字；"
        f"Coding case 首次 ≤ {CODING_OUTPUT_MAX_CHARS} 字",
        flush=True,
    )
    if args.mode == "rag_id":
        print(
            f"RAG 语料目录: {args.rag_documents_dir.expanduser().resolve()}，"
            f"按 source_docs ID 精确读取，"
            f"context: {args.rag_context_max_chars} 字",
            flush=True,
        )
    elif args.mode == "web":
        print(
            f"Web Search: Tavily，max_queries: {args.web_max_queries}，"
            f"每条结果数: {args.web_max_results}，"
            f"context: {args.web_context_max_chars} 字",
            flush=True,
        )

    try:
        answer_flow = _build_answer_flow(args)
        stats = enrich_jsonl(
            input_path=args.input,
            output_path=output_path,
            answer_flow=answer_flow,
            workers=args.workers,
            clear_output=args.clear,
        )
    except (FileNotFoundError, TypeError, ValueError, RuntimeError) as exc:
        print(f"错误: {exc}", file=sys.stderr, flush=True)
        return 1

    print(f"结果文件: {stats.output_path}", flush=True)
    return 1 if stats.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
