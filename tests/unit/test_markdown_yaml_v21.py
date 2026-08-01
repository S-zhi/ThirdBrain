"""Schema 2.1 Markdown 预处理派生文本测试。"""

from dataclasses import replace
from pathlib import Path

from config import load_config
from src.script.extract_docs import build_multimodal_user_content
from src.script.markdown_yaml_v21 import (
    _is_collection_empty,
    _is_value_empty,
    extract_resources,
    preprocess_markdown,
    run_pipeline,
)

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

支持 int16\\_t、PIPE\\_V 和 16\\*16，代码标识为 `escaped\\_name`。

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
    assert "`escaped\\_name`" in preprocess
    assert "|src|int16_t|" in preprocess
    assert 'const char* pattern = "\\\\*";' in preprocess
    assert evidence["tables"][0]["rows"][0][1] == "int16_t"
    assert evidence["code_blocks"][0]["content"] == 'const char* pattern = "\\\\*";'


def test_preprocess_preserves_semantic_blocks_and_link_anchors() -> None:
    """表格、代码和链接锚文本必须保留，链接地址必须删除。"""
    markdown = """# asc_get_ffts_base_addr

| 产品 | 是否支持 |
| --- | --- |
| Atlas A3 | √ |

```cpp
int64_t asc_get_ffts_base_addr();
```

调用 [asc_set_ffts_base_addr](https://example.com/asc_set_ffts_base_addr.md) 后使用。

另见 [系统变量][sys-var]，裸链接 <https://example.com/bare>。

[sys-var]: https://example.com/system-variable
"""

    preprocess, _ = preprocess_markdown(markdown, load_config().markdown_to_yaml)

    assert "| Atlas A3 | √ |" in preprocess
    assert "int64_t asc_get_ffts_base_addr();" in preprocess
    assert "asc_set_ffts_base_addr" in preprocess
    assert "另见 系统变量，裸链接 。" in preprocess
    assert "https://example.com" not in preprocess
    assert "调用 asc_set_ffts_base_addr 后使用。" in preprocess


def test_preprocess_preserves_html_tables_in_lossless_mode() -> None:
    """关闭表格删除后，HTML 表格也必须留在派生正文中。"""
    markdown = "# example\n\n<table><tr><td>real</td></tr></table>\n"

    preprocess, _ = preprocess_markdown(markdown, load_config().markdown_to_yaml)

    assert "<table><tr><td>real</td></tr></table>" in preprocess


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


# ---------------------------------------------------------------------------
# "AI 只填空值" 硬约束测试
# ---------------------------------------------------------------------------


def _loaded_config_with_text_ai_only() -> object:
    """加载默认配置，但禁用图片理解，避免图片节点干扰文本 AI 路径。"""
    loaded = load_config().markdown_to_yaml
    return replace(
        loaded,
        ai=replace(
            loaded.ai,
            image_understanding=replace(loaded.ai.image_understanding, enabled=False),
        ),
    )


def test_value_and_collection_empty_helpers() -> None:
    """空判断工具必须同时覆盖 None、纯空白字符串、空列表和 value 包装。"""
    assert _is_value_empty(None) is True
    assert _is_value_empty("") is True
    assert _is_value_empty("   \n\t ") is True
    assert _is_value_empty({"value": "", "is_ai": False}) is True
    assert _is_value_empty({"value": None, "is_ai": False}) is True
    assert _is_value_empty("hello") is False
    assert _is_value_empty({"value": "x", "is_ai": True}) is False
    assert _is_value_empty({"value": "x", "is_ai": False}) is False

    assert _is_collection_empty(None) is True
    assert _is_collection_empty([]) is True
    assert _is_collection_empty([{"a": 1}]) is False


def test_ai_does_not_overwrite_existing_text_fields() -> None:
    """AI 试图给已有非空文本字段写入新值时，原值必须保持不变。"""
    markdown = "# example\n\n无任何结构化数据。\n"
    config = _loaded_config_with_text_ai_only()

    # 先跑一次，让骨架构造完成
    base_result = run_pipeline(
        markdown=markdown,
        source_path=Path("example.md"),
        source_url=None,
        hints=PIPELINE_HINTS,
        config=config,
        project_root=Path.cwd(),
        ai_call=lambda _p: "document_updates: []\n",
        image_ai_call=None,
    )
    document = base_result.document

    # 模拟骨架在 AI 跑之前已经通过非 AI 路径填好了这些字段（确定性证据）
    document["documents"][0]["name"] = "real_name"
    document["documents"][0]["use"]["summary"] = {"value": "真实摘要", "is_ai": False}
    document["documents"][0]["use"]["description"] = {"value": "真实描述", "is_ai": False}
    document["documents"][0]["use"]["category"] = {"value": "function", "is_ai": False}
    document["documents"][0]["use"]["function_details"]["signature"] = {
        "value": "int real_name(int x);",
        "is_ai": False,
    }

    from src.script.markdown_yaml_v21 import (
        _filter_enabled_nodes_for_empty_slots,
        merge_ai_updates,
        parse_ai_updates,
    )

    # 验证 _filter 已经把 5 个字段排除（无空槽位）
    filtered = _filter_enabled_nodes_for_empty_slots(document, config)
    for blocked in (
        "documents[].name",
        "documents[].use.summary",
        "documents[].use.description",
        "documents[].use.category",
        "documents[].use.function_details.signature",
    ):
        assert blocked not in filtered, f"{blocked} 不应出现在 enabled_nodes 中"

    # 构造一个恶意的 AI 响应，试图覆盖所有已有值
    malicious_response = parse_ai_updates(
        """document_updates:
  - document_index: 0
    name: OVERWRITTEN
    summary: OVERWRITTEN
    description: OVERWRITTEN
    category: data_structure
    signature: OVERWRITTEN()
"""
    )

    # merge 阶段必须再次拒绝（不信任 _filter）
    merge_ai_updates(document, malicious_response, config)

    target = document["documents"][0]
    assert target["name"] == "real_name"
    assert target["use"]["summary"] == {"value": "真实摘要", "is_ai": False}
    assert target["use"]["description"] == {"value": "真实描述", "is_ai": False}
    assert target["use"]["category"] == {"value": "function", "is_ai": False}
    assert target["use"]["function_details"]["signature"] == {
        "value": "int real_name(int x);",
        "is_ai": False,
    }


def test_ai_fills_empty_text_fields_with_evidence() -> None:
    """空字段被合法证据填充时，必须能正常写入。"""
    markdown = (
        "# example\n\n"
        "该函数用于计算。\n\n"
        "```cpp\n"
        "int example_compute(int x);\n"
        "```\n\n"
        "| 参数 | 类型 | 说明 |\n"
        "| --- | --- | --- |\n"
        "| x | int | 输入值 |\n"
    )
    config = _loaded_config_with_text_ai_only()

    def ai_call(_prompt: str) -> str:
        return """document_updates:
  - document_index: 0
    summary: 用于计算
    description: 该函数用于计算。
    signature: int example_compute(int x);
    input_parameters:
      - name: x
        type: int
        description: 输入值
"""

    result = run_pipeline(
        markdown=markdown,
        source_path=Path("example.md"),
        source_url=None,
        hints=PIPELINE_HINTS,
        config=config,
        project_root=Path.cwd(),
        ai_call=ai_call,
        image_ai_call=None,
    )

    document = result.document["documents"][0]
    use = document["use"]
    assert use["summary"]["value"] == "用于计算"
    assert use["summary"]["is_ai"] is False  # 原文能找到，是 evidence
    assert use["description"]["value"] == "该函数用于计算。"
    assert use["function_details"]["signature"]["value"] == "int example_compute(int x);"
    assert use["function_details"]["input_parameters"][0]["name"] == "x"


def test_pipeline_skips_ai_when_no_empty_slots() -> None:
    """当前文档没有任何空槽位时，run_pipeline 不得调用文本 AI。"""
    markdown = "# example\n\n无任何结构化数据。\n"
    config = _loaded_config_with_text_ai_only()

    # 先跑一次让骨架就绪，然后手工把所有文本槽位填上（确定性证据）
    base_result = run_pipeline(
        markdown=markdown,
        source_path=Path("example.md"),
        source_url=None,
        hints=PIPELINE_HINTS,
        config=config,
        project_root=Path.cwd(),
        ai_call=lambda _p: "document_updates: []\n",
        image_ai_call=None,
    )
    document = base_result.document
    document["documents"][0]["use"]["summary"] = {"value": "已填", "is_ai": False}
    document["documents"][0]["use"]["description"] = {"value": "已填", "is_ai": False}
    document["documents"][0]["use"]["product_support"] = [
        {"product": "Atlas A2", "supported": True, "is_ai": False}
    ]
    document["documents"][0]["use"]["prerequisites"] = [{"value": "已填", "is_ai": False}]
    document["documents"][0]["use"]["function_details"]["input_parameters"] = [
        {"name": "x", "type": "int", "description": "已填", "is_ai": False}
    ]
    document["documents"][0]["use"]["function_details"]["output_parameters"] = [
        {"name": "y", "type": "int", "description": "已填", "is_ai": False}
    ]
    document["documents"][0]["use"]["function_details"]["signature"] = {
        "value": "int example(int x);",
        "is_ai": False,
    }
    document["documents"][0]["use"]["data_structure"]["fields"] = [
        {"name": "f", "type": "int", "description": "已填", "is_ai": False}
    ]
    document["documents"][0]["use"]["examples"] = [{"value": "已填", "is_ai": False}]

    from src.script.markdown_yaml_v21 import (
        _filter_enabled_nodes_for_empty_slots,
        _has_empty_slot_for_node,
    )

    # 校验：当前文档下所有节点都没有空槽位
    for path in (
        "documents[].name",
        "documents[].use.summary",
        "documents[].use.category",
        "documents[].use.description",
        "documents[].use.product_support",
        "documents[].use.prerequisites",
        "documents[].use.function_details.input_parameters",
        "documents[].use.function_details.output_parameters",
        "documents[].use.function_details.signature",
        "documents[].use.data_structure.fields",
        "documents[].use.examples",
    ):
        assert _has_empty_slot_for_node(document, path) is False, f"{path} 应无空槽位"

    filtered = _filter_enabled_nodes_for_empty_slots(document, config)
    # 图片节点由 image_ai_call 独立处理（已在 _image_resources_for_ai 中过滤），
    # 因此 _filter 仍会保留图片节点，但所有文本槽位必须已被剔除。
    text_paths = {
        path
        for path in filtered
        if path != "resources[].raw.alt" and path != "resources[].raw.title"
    }
    assert text_paths == set(), f"所有文本槽位应已被剔除，仍有: {text_paths}"


def test_ai_does_not_replace_existing_product_support() -> None:
    """product_support 已有内容时，AI 试图整体替换必须被拒。"""
    markdown = "# example\n\n无支持矩阵段落。\n"
    config = _loaded_config_with_text_ai_only()

    # 第一次跑：让 AI 写入 product_support
    first_ai_calls = {"count": 0}

    def first_ai_call(prompt: str) -> str:
        first_ai_calls["count"] += 1
        return """document_updates:
  - document_index: 0
    product_support:
      - product: Atlas A3
        supported: true
"""

    first_result = run_pipeline(
        markdown=markdown,
        source_path=Path("example.md"),
        source_url=None,
        hints=PIPELINE_HINTS,
        config=config,
        project_root=Path.cwd(),
        ai_call=first_ai_call,
        image_ai_call=None,
    )
    # Atlas A3 不在原文中，因此被 require_evidence=true 拒绝
    # 让我们换一个真实存在于原文中或 allow_generate 的方式。
    # 简化：直接在文档骨架里手工注入 product_support 再重跑。
    first_doc = first_result.document["documents"][0]
    first_doc["use"]["product_support"] = [
        {"product": "Atlas A2", "supported": True, "is_ai": False}
    ]
    # 写一个能让 _filter 把它视为"非空"的版本，重新跑 AI 试图覆盖。
    # 重新构造一个 run：让骨架继承 first_result 的状态。
    # 这里使用更直接的方式：直接调用 build_ai_prompt + merge 路径。
    from src.script.markdown_yaml_v21 import (
        _filter_enabled_nodes_for_empty_slots,
        merge_ai_updates,
        parse_ai_updates,
    )

    # 构造一个"已存在 product_support"的最终文档
    document = first_result.document
    document["documents"][0]["use"]["product_support"] = [
        {"product": "Atlas A2", "supported": True, "is_ai": False}
    ]
    # 重新过滤应该把 product_support 节点视为"无空槽位"
    filtered = _filter_enabled_nodes_for_empty_slots(document, config)
    assert "documents[].use.product_support" not in filtered

    # 即便 AI 返回了新列表，merge 阶段也必须拒绝
    merge_ai_updates(
        document,
        parse_ai_updates(
            """document_updates:
  - document_index: 0
    product_support:
      - product: REPLACED
        supported: false
"""
        ),
        config,
    )
    assert document["documents"][0]["use"]["product_support"] == [
        {"product": "Atlas A2", "supported": True, "is_ai": False}
    ]


def test_ai_does_not_replace_existing_input_parameters() -> None:
    """input_parameters 已有内容时，AI 试图整体替换必须被拒。"""
    markdown = "# example\n\n无参数表段落。\n"
    config = _loaded_config_with_text_ai_only()
    from src.script.markdown_yaml_v21 import (
        _filter_enabled_nodes_for_empty_slots,
        merge_ai_updates,
        parse_ai_updates,
    )

    # 先跑一遍获得一个 document，然后把 input_parameters 注入
    first_result = run_pipeline(
        markdown=markdown,
        source_path=Path("example.md"),
        source_url=None,
        hints=PIPELINE_HINTS,
        config=config,
        project_root=Path.cwd(),
        ai_call=lambda _p: "document_updates: []\n",
        image_ai_call=None,
    )
    document = first_result.document
    document["documents"][0]["use"]["function_details"]["input_parameters"] = [
        {"name": "existing", "type": "int", "description": "已存在", "is_ai": False}
    ]

    filtered = _filter_enabled_nodes_for_empty_slots(document, config)
    assert "documents[].use.function_details.input_parameters" not in filtered

    merge_ai_updates(
        document,
        parse_ai_updates(
            """document_updates:
  - document_index: 0
    input_parameters:
      - name: replaced
        type: int
        description: 被覆盖
"""
        ),
        config,
    )
    assert document["documents"][0]["use"]["function_details"]["input_parameters"] == [
        {"name": "existing", "type": "int", "description": "已存在", "is_ai": False}
    ]


def test_image_merge_does_not_overwrite_existing_alt_title() -> None:
    """图片 alt/title 已有值时，AI 返回新值必须被 merge 阶段拒绝。"""
    markdown = (
        "# example\n\n"
        '已有 alt：![原始 alt 文本](https://example.com/a.png "原始 title")\n'
        "新图：![](https://example.com/b.png)\n"
    )
    loaded = load_config().markdown_to_yaml
    resource_nodes = {
        path: node for path, node in loaded.ai.nodes.items() if path.startswith("resources[]")
    }
    config = replace(loaded, ai=replace(loaded.ai, nodes=resource_nodes))

    def image_ai_call(_prompt: str, _images: list[dict[str, str]]) -> str:
        return """image_updates:
  - resource_id: res_img_001
    alt: 不应覆盖
    title: 不应覆盖
  - resource_id: res_img_002
    alt: 数据流图
    title: 流程示意
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

    resources = result.document["source"]["resources"]
    # 第一张图 alt/title 都已有，AI 不能覆盖
    assert resources[0]["raw"]["alt"] == {"value": "原始 alt 文本", "is_ai": False}
    assert resources[0]["raw"]["title"] == {"value": "原始 title", "is_ai": False}
    # 第二张图 alt/title 都空，AI 可正常填充
    assert resources[1]["raw"]["alt"] == {"value": "数据流图", "is_ai": True}
    assert resources[1]["raw"]["title"] == {"value": "流程示意", "is_ai": True}
