"""Schema 2.1 Markdown 预处理派生文本测试。"""

from dataclasses import replace
from pathlib import Path

from config import load_config
from src.script.extract_docs import build_multimodal_user_content
from src.script.markdown_yaml_v21 import extract_resources, preprocess_markdown, run_pipeline

PIPELINE_HINTS = {
    "name": "example",
    "namespace": "AscendC.910beta3",
    "version": "910beta3",
    "language": "cpp",
    "category": "function",
}


def test_preprocess_unescapes_markdown_text_but_preserves_code() -> None:
    """正文和表格证据应去除标点转义，代码内容应原样保留。"""
    markdown = """# example

支持 int16\\_t、PIPE\\_V 和 16\\*16。

|参数|类型|
|---|---|
|src|int16\\_t|

```cpp
const char* pattern = "\\\\*";
```
"""

    preprocess, evidence = preprocess_markdown(markdown, load_config().markdown_to_yaml)

    assert "int16_t" in preprocess
    assert "PIPE_V" in preprocess
    assert "16*16" in preprocess
    assert "\\_" not in preprocess
    assert evidence["tables"][0]["rows"][0][1] == "int16_t"
    assert evidence["code_blocks"][0]["content"] == 'const char* pattern = "\\\\*";'


def test_preprocess_preserves_required_latex_backslashes() -> None:
    """普通文本去转义时不应损坏数学公式中的 LaTeX 命令。"""
    markdown = """# example

类型为 int16\\_t。

$$
dst_i = \\frac{src_i}{scale\\_value}
$$
"""

    preprocess, _ = preprocess_markdown(markdown, load_config().markdown_to_yaml)

    assert "int16_t" in preprocess
    assert r"\frac{src_i}{scale\_value}" in preprocess


def test_resources_unescape_display_text() -> None:
    """资源的 alt、title 和链接锚文本应使用无反斜杠的派生值。"""
    markdown = (
        '![int16\\_t](https://example.com/a.png "PIPE\\_V") [asc\\_api](https://example.com/api)'
    )

    resources, _ = extract_resources(
        markdown,
        "https://example.com/source.md",
        Path("source.md"),
    )

    assert resources[0]["raw"]["alt"]["value"] == "int16_t"
    assert resources[0]["raw"]["title"]["value"] == "PIPE_V"
    assert resources[1]["raw"]["anchor_text"]["value"] == "asc_api"


def test_pipeline_preserves_audit_source_and_cleans_preprocess() -> None:
    """流水线应只规范化派生正文，不改动审计用原始 Markdown。"""
    markdown = "# example\n\n输入类型为 int16\\_t。\n"
    loaded = load_config().markdown_to_yaml
    config = replace(loaded, ai=replace(loaded.ai, enabled=False))

    result = run_pipeline(
        markdown=markdown,
        source_path=Path("example.md"),
        source_url=None,
        hints=PIPELINE_HINTS,
        config=config,
        project_root=Path.cwd(),
        ai_call=None,
    )

    assert "int16\\_t" in result.document["source"]["source_markdown"]
    assert "int16_t" in result.document["source"]["preprocess_markdown"]
    assert "\\_" not in result.document["source"]["preprocess_markdown"]


def test_image_understanding_sends_url_and_fills_missing_metadata() -> None:
    """图片开关启用时应发送真实 URL，并只用 AI 结果补齐空字段。"""
    markdown = (
        "# example\n\n"
        '已有说明：![原始 alt](https://example.com/formula.png "")\n\n'
        "待识别：![](https://example.com/diagram.png)\n"
    )
    loaded = load_config().markdown_to_yaml
    resource_nodes = {
        path: node for path, node in loaded.ai.nodes.items() if path.startswith("resources[]")
    }
    config = replace(loaded, ai=replace(loaded.ai, nodes=resource_nodes))
    captured: list[tuple[str, list[dict[str, str]]]] = []

    def image_ai_call(prompt: str, images: list[dict[str, str]]) -> str:
        captured.append((prompt, images))
        return """image_updates:
  - resource_id: res_img_001
    alt: 不应覆盖原始 alt
    title: 一元公式
  - resource_id: res_img_002
    alt: 数据从源布局转换为五维目标布局
    title: 布局转换示意图
"""

    result = run_pipeline(
        markdown=markdown,
        source_path=Path("example.md"),
        source_url="https://example.com/example.md",
        hints=PIPELINE_HINTS,
        config=config,
        project_root=Path.cwd(),
        ai_call=None,
        image_ai_call=image_ai_call,
    )

    assert len(captured) == 1
    prompt, images = captured[0]
    assert "你正在为另一个代码 Agent 解释 API 文档中的图片" in prompt
    assert [image["url"] for image in images] == [
        "https://example.com/formula.png",
        "https://example.com/diagram.png",
    ]
    resources = result.document["source"]["resources"]
    assert resources[0]["raw"]["alt"] == {"value": "原始 alt", "is_ai": False}
    assert resources[0]["raw"]["title"] == {"value": "一元公式", "is_ai": True}
    assert resources[1]["raw"]["alt"] == {
        "value": "数据从源布局转换为五维目标布局",
        "is_ai": True,
    }
    assert resources[1]["raw"]["title"] == {"value": "布局转换示意图", "is_ai": True}
    assert result.image_prompts == [prompt]
    assert len(result.image_responses) == 1


def test_disabled_image_understanding_never_calls_multimodal_model() -> None:
    """图片处理关闭时即使文档含图片，也不能调用多模态模型。"""
    loaded = load_config().markdown_to_yaml
    image_config = replace(loaded.ai.image_understanding, enabled=False)
    config = replace(
        loaded,
        ai=replace(loaded.ai, nodes={}, image_understanding=image_config),
    )

    def forbidden_call(_prompt: str, _images: list[dict[str, str]]) -> str:
        raise AssertionError("图片处理关闭后不应调用模型")

    result = run_pipeline(
        markdown="# example\n\n![](https://example.com/a.png)\n",
        source_path=Path("example.md"),
        source_url="https://example.com/example.md",
        hints=PIPELINE_HINTS,
        config=config,
        project_root=Path.cwd(),
        ai_call=None,
        image_ai_call=forbidden_call,
    )

    assert result.image_prompts == []
    assert result.document["source"]["resources"][0]["raw"]["alt"]["value"] is None


def test_image_field_nodes_control_which_metadata_is_filled() -> None:
    """图片总开关下的 alt/title 字段开关仍应分别生效。"""
    loaded = load_config().markdown_to_yaml
    nodes = {
        path: replace(node, enabled=False) if path == "resources[].raw.alt" else node
        for path, node in loaded.ai.nodes.items()
        if path.startswith("resources[]")
    }
    config = replace(loaded, ai=replace(loaded.ai, nodes=nodes))

    def image_ai_call(prompt: str, _images: list[dict[str, str]]) -> str:
        assert "fields_to_fill:\n  - title" in prompt
        return """image_updates:
  - resource_id: res_img_001
    alt: 不应写入
    title: 参数关系图
"""

    result = run_pipeline(
        markdown="# example\n\n![](https://example.com/a.png)\n",
        source_path=Path("example.md"),
        source_url="https://example.com/example.md",
        hints=PIPELINE_HINTS,
        config=config,
        project_root=Path.cwd(),
        ai_call=None,
        image_ai_call=image_ai_call,
    )

    raw = result.document["source"]["resources"][0]["raw"]
    assert raw["alt"] == {"value": None, "is_ai": False}
    assert raw["title"] == {"value": "参数关系图", "is_ai": True}


def test_multimodal_payload_contains_image_url_blocks() -> None:
    """发送层必须把每张图片放进 image_url 内容块，而不是只写在提示词中。"""
    content = build_multimodal_user_content(
        "解释图片",
        [
            {
                "resource_id": "res_img_001",
                "url": "https://example.com/a.png",
            }
        ],
    )

    assert content == [
        {"type": "text", "text": "解释图片"},
        {
            "type": "image_url",
            "image_url": {"url": "https://example.com/a.png"},
        },
    ]
