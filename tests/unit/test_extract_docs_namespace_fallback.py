"""extract_docs 路径感知的 namespace 退讨规则测试。"""

from pathlib import Path

from src.script.extract_docs import (
    _default_namespace_from_path,
    _default_namespace_segment,
    resolve_authoritative_hints,
)


def _args(**overrides: object) -> object:
    """构造一个只含 HINT_FIELDS 字段的 Namespace 替身。"""
    from types import SimpleNamespace

    values: dict[str, object] = {
        "chunk_id": None,
        "name": None,
        "namespace": None,
        "version": None,
        "language": None,
        "category": None,
        "module": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_default_namespace_segment_known_paths() -> None:
    """已知路径段映射到稳定的 namespace 中段。"""
    assert _default_namespace_segment("SIMD_API") == "simd_op"
    assert _default_namespace_segment("SIMT_API") == "simt_op"
    assert _default_namespace_segment("AI_CPU_API") == "aicpu_op"
    assert _default_namespace_segment("Utils_API") == "utils"
    assert _default_namespace_segment("附录") == "appendix"
    assert _default_namespace_segment("Ascend_C") == "ascendc"


def test_default_namespace_segment_unknown_paths_normalize() -> None:
    """未知路径段按小写 + 替换非字母数字来兜底，绝不返回空。"""
    assert _default_namespace_segment("float类型数学库函数") == "float"
    assert _default_namespace_segment("Custom-Category!") == "custom_category"
    assert _default_namespace_segment("!!!") == "unknown"


def test_default_namespace_from_path_includes_version() -> None:
    """version 已知时把 version 拼成 namespace 的最后一段。"""
    assert (
        _default_namespace_from_path(Path("API参考/SIMT_API/isGlobal.md"), "910beta3")
        == "com.huawei.cann.simt_op.910beta3"
    )
    assert (
        _default_namespace_from_path(Path("API参考/AI_CPU_API/printf.md"), "910beta3")
        == "com.huawei.cann.aicpu_op.910beta3"
    )


def test_default_namespace_from_path_without_version() -> None:
    """version 未知时仅返回产品段，仍然非空。"""
    assert (
        _default_namespace_from_path(Path("API参考/Utils_API/foo.md"), None)
        == "com.huawei.cann.utils"
    )
    assert (
        _default_namespace_from_path(Path("API参考/附录/internal.md"), None)
        == "com.huawei.cann.appendix"
    )


def test_default_namespace_from_path_outside_api_reference() -> None:
    """非 API参考 路径返回 None，由调用方决定是否进一步兜底。"""
    assert _default_namespace_from_path(Path("other/SIMT_API/x.md"), "910beta3") is None


def test_resolve_authoritative_hints_simd_path_unchanged() -> None:
    """SIMD_API 路径继续走 ascendc 命名空间特化逻辑。"""
    markdown = "# asc_relu\n\n> 来源: https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/910beta3/API/ascendcopapi/...\n"
    hints = resolve_authoritative_hints(
        _args(),
        Path("API参考/SIMD_API/C_API/Reg矢量计算/reg_vector/asc_relu.md"),
        markdown,
        source_url="https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/910beta3/API/ascendcopapi/context/reg/reg_vector/asc_relu.md",
    )
    assert hints["namespace"] == "com.huawei.cann.ascendc.op.910beta3"
    assert hints["version"] == "910beta3"


def test_resolve_authoritative_hints_simt_path_uses_fallback() -> None:
    """SIMT_API 路径触发默认退讨规则。"""
    markdown = "# isGlobal\n\n> 来源: https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/910beta3/API/ascendcopapi/...\n"
    hints = resolve_authoritative_hints(
        _args(),
        Path("API参考/SIMT_API/地址空间谓词函数/isGlobal.md"),
        markdown,
        source_url="https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/910beta3/API/ascendcopapi/atlasascendc_api_07_xxxxx.html",
    )
    assert hints["namespace"] == "com.huawei.cann.simt_op.910beta3"
    assert hints["version"] == "910beta3"


def test_resolve_authoritative_hints_ai_cpu_path_uses_fallback() -> None:
    """AI_CPU_API 路径触发默认退讨规则。"""
    markdown = "# printf\n\n> 来源: https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/910beta3/API/ascendcopapi/...\n"
    hints = resolve_authoritative_hints(
        _args(),
        Path("API参考/AI_CPU_API/printf.md"),
        markdown,
        source_url="https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/910beta3/API/ascendcopapi/atlasascendc_api_07_00166.html",
    )
    assert hints["namespace"] == "com.huawei.cann.aicpu_op.910beta3"


def test_resolve_authoritative_hints_附录_path_uses_fallback_without_version() -> None:
    """附录路径无 source_url 时，仍能通过退讨得到非空 namespace。"""
    markdown = "# 内部关联接口\n"
    hints = resolve_authoritative_hints(
        _args(),
        Path("API参考/附录/内部关联接口.md"),
        markdown,
        source_url=None,
    )
    assert hints["namespace"] == "com.huawei.cann.appendix"


def test_resolve_authoritative_hints_cli_namespace_wins() -> None:
    """CLI 显式给的 namespace 优先级最高，不被退讨规则覆盖。"""
    hints = resolve_authoritative_hints(
        _args(namespace="custom.cli.namespace"),
        Path("API参考/SIMT_API/foo.md"),
        "# foo\n",
        source_url=None,
    )
    assert hints["namespace"] == "custom.cli.namespace"
