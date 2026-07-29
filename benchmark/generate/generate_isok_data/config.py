"""
配置文件：LLM API 地址、模型名、路径、策略参数等。
"""

import os
import re
import threading
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# 自动加载项目根目录下的 .env 文件
_env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=_env_path)


def _first_env(*names: str, default: str = "") -> str:
    """按优先级读取第一个非空环境变量。"""
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


def _normalize_minimax_base_url(value: str) -> str:
    """把 MiniMax 完整接口地址规范化为 OpenAI SDK 所需的 /v1 基地址。"""
    normalized = value.rstrip("/")
    for suffix in ("/chat/completions", "/text/chatcompletion_v2"):
        normalized = normalized.removesuffix(suffix)
    return normalized


# ============================================================
# LLM API 配置
# ============================================================
LLM_BASE_URL = _normalize_minimax_base_url(
    _first_env(
        "MINIMAX_BASE_URL",
        "LLM_BASE_URL",
        default="https://api.minimaxi.com/v1",
    )
)
LLM_API_KEY = _first_env("MINIMAX_API_KEY", "LLM_API_KEY")
LLM_MODEL_NAME = _first_env(
    "MINIMAX_MODEL",
    "LLM_MODEL_NAME",
    default="MiniMax-M2.7-highspeed",
)
LLM_TEMPERATURE = float(
    _first_env("MINIMAX_TEMPERATURE", "LLM_TEMPERATURE", default="0.7")
)
LLM_MAX_TOKENS = int(
    _first_env("MINIMAX_MAX_TOKENS", "LLM_MAX_TOKENS", default="40960")
)
LLM_TIMEOUT = float(_first_env("MINIMAX_TIMEOUT", "LLM_TIMEOUT", default="120"))
LLM_MAX_RETRIES = int(_first_env("MINIMAX_MAX_RETRIES", "LLM_MAX_RETRIES", default="4"))

# ============================================================
# 路径配置
# ============================================================
PROJECT_ROOT = Path(__file__).parent.resolve()
PROMPTS_DIR = PROJECT_ROOT / "prompts"
DEFAULT_DOCS_DIR = PROJECT_ROOT / "md"
DEFAULT_OUTPUT = PROJECT_ROOT / "output.jsonl"

# ============================================================
# 扫描策略配置
# ============================================================
# 浅扫：每个文件最多读取前 N 行，或直到遇到第一个二级标题
SCAN_MAX_LINES = 80

# ============================================================
# 选取策略配置
# ============================================================
# 对比模式概率（0.0 ~ 1.0）
COMPARISON_MODE_PROB = 0.10
# 随机模式下，扩展为多选的子概率
RANDOM_EXPAND_PROB = 0.10
# 多选时最多追加的文档数
MAX_EXTRA_DOCS = 2

# ============================================================
# 生成策略配置
# ============================================================
# 默认每轮生成问题数
DEFAULT_QUESTION_COUNT = 1

# ============================================================
# 评估说明字数上限
# ============================================================
EVAL_NOTE_MAX_CHARS = 200


def load_prompt(filename: str) -> str:
    """从 prompts/ 目录加载提示词模板。"""
    filepath = PROMPTS_DIR / filename
    if not filepath.exists():
        raise FileNotFoundError(f"提示词文件不存在: {filepath}")
    return filepath.read_text(encoding="utf-8")


# 每个生产线程复用自己的同步客户端，避免高并发时反复建立连接。
_client_state = threading.local()


def _get_minimax_client() -> Any:
    """获取当前线程复用的 MiniMax OpenAI 兼容客户端。"""
    client = getattr(_client_state, "client", None)
    if client is not None:
        return client

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("需要安装 openai 库：请在项目根目录执行 uv sync。") from exc

    client = OpenAI(
        base_url=LLM_BASE_URL,
        api_key=LLM_API_KEY,
        timeout=LLM_TIMEOUT,
        max_retries=LLM_MAX_RETRIES,
    )
    _client_state.client = client
    return client


def _strip_thinking_content(content: str) -> str:
    """移除 MiniMax 兼容响应 content 中可能出现的思考标签。"""
    return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()


def llm_call(prompt: str, system_prompt: str = "You are a helpful assistant.") -> str:
    """通过 OpenAI 兼容接口调用 MiniMax 并返回清理后的文本。"""
    if not LLM_API_KEY or LLM_API_KEY == "sk-your-api-key-here":
        raise RuntimeError(
            "请设置 MINIMAX_API_KEY（兼容旧的 LLM_API_KEY），"
            "或使用 --mock 跳过模型调用。"
        )

    try:
        client = _get_minimax_client()
        response = client.chat.completions.create(
            model=LLM_MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            temperature=LLM_TEMPERATURE,
            max_completion_tokens=LLM_MAX_TOKENS,
        )
        if not response.choices:
            raise RuntimeError("MiniMax 返回体缺少 choices[0]")

        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise RuntimeError("MiniMax 输出达到长度上限，请提高 MINIMAX_MAX_TOKENS")

        content = choice.message.content
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("MiniMax 返回内容为空")

        cleaned = _strip_thinking_content(content)
        if not cleaned:
            raise RuntimeError("MiniMax 返回内容仅包含思考过程，没有最终答案")
        return cleaned
    except Exception as e:
        if isinstance(e, RuntimeError):
            raise
        raise RuntimeError(f"MiniMax API 调用失败: {type(e).__name__}: {e}") from e
