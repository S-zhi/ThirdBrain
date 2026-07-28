"""
配置文件：LLM API 地址、模型名、路径、策略参数等。
"""

import os
from pathlib import Path

# ============================================================
# LLM API 配置
# ============================================================
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "sk-your-api-key-here")
LLM_MODEL_NAME = os.environ.get("LLM_MODEL_NAME", "gpt-4o-mini")
LLM_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.7"))
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "40960"))
LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "120"))

# ============================================================
# 路径配置
# ============================================================
PROJECT_ROOT = Path(__file__).parent.resolve()
PROMPTS_DIR = PROJECT_ROOT / "prompts"
DEFAULT_DOCS_DIR = PROJECT_ROOT / "examples"
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


# ============================================================
# LLM 调用封装
# ============================================================

def llm_call(prompt: str, system_prompt: str = "You are a helpful assistant.") -> str:
    """
    调用 LLM API（OpenAI 兼容接口）。
    
    Args:
        prompt: 用户提示词
        system_prompt: 系统提示词
    
    Returns:
        LLM 响应文本
    
    Raises:
        RuntimeError: API 调用失败时抛出
    """
    try:
        from openai import OpenAI
    except ImportError:
        raise RuntimeError(
            "需要安装 openai 库: pip install openai\n"
            "如果不需要实际调用 LLM，请使用 --mock 参数运行。"
        )

    if LLM_API_KEY == "sk-your-api-key-here":
        raise RuntimeError(
            "请先配置 LLM API Key！\n"
            "方式1: 设置环境变量 LLM_API_KEY\n"
            "方式2: 修改 config.py 中的 LLM_API_KEY\n"
            "方式3: 使用 --mock 参数跳过 LLM 调用"
        )

    client = OpenAI(
        base_url=LLM_BASE_URL,
        api_key=LLM_API_KEY,
    )

    try:
        response = client.chat.completions.create(
            model=LLM_MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            temperature=LLM_TEMPERATURE,
            max_tokens=LLM_MAX_TOKENS,
            timeout=LLM_TIMEOUT,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        raise RuntimeError(f"LLM API 调用失败: {type(e).__name__}: {e}") from e
