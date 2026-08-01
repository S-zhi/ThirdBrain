"""目录级 YAML → Zvec 底层摄取接口测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.script import ingest


def _write_yaml(path: Path, *, chunk_id: str, name: str) -> None:
    """写入一份最小合法 Schema 1.0 YAML。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                'schema_version: "1.0"',
                f"chunk_id: {chunk_id}",
                f"name: {name}",
                "namespace: com.example.api.v1",
                "description: directory ingest test",
            ]
        ),
        encoding="utf-8",
    )


def test_discover_yaml_directory_recurses_every_level(tmp_path: Path) -> None:
    """根、子、孙目录中的 yaml/yml 都应被发现，其他文件应被忽略。"""
    root_yaml = tmp_path / "root.yaml"
    child_yaml = tmp_path / "child" / "child.yml"
    descendant_yaml = tmp_path / "child" / "grandchild" / "descendant.YAML"
    _write_yaml(root_yaml, chunk_id="root", name="Root")
    _write_yaml(child_yaml, chunk_id="child", name="Child")
    _write_yaml(descendant_yaml, chunk_id="descendant", name="Descendant")
    (tmp_path / "child" / "ignored.txt").write_text("ignored", encoding="utf-8")

    discovered = ingest.discover_yaml_directory(tmp_path)

    assert discovered == sorted(
        [str(root_yaml.resolve()), str(child_yaml.resolve()), str(descendant_yaml.resolve())]
    )


def test_ingest_yaml_directory_indexes_all_files_as_one_batch(
    tmp_path: Path,
    monkeypatch,
    isolated_config,
) -> None:
    """底层接口应把所有层级文件合并为一次 Zvec 批量写入。"""
    source_directory = tmp_path / "source"
    _write_yaml(source_directory / "root.yaml", chunk_id="root", name="Root")
    _write_yaml(source_directory / "a" / "child.yaml", chunk_id="child", name="Child")
    _write_yaml(
        source_directory / "a" / "b" / "descendant.yml",
        chunk_id="descendant",
        name="Descendant",
    )
    captured: dict[str, Any] = {}

    def fake_insert(records, collection, profile):
        captured["chunk_ids"] = [record.chunk_id for record in records]
        captured["collection"] = collection
        captured["profile"] = profile
        return {"ok": len(records), "errors": []}

    monkeypatch.setattr(ingest, "insert_vector_documents", fake_insert)

    result = ingest.ingest_yaml_directory(
        source_directory,
        collection="recursive_ingest_test",
        record_directory=tmp_path / "records",
        preview_limit=0,
    )

    assert result.record.status == "succeeded"
    assert result.record.sub_directory == "**"
    assert result.record.discovered_count == 3
    assert result.record.parsed_count == 3
    assert result.record.indexed_count == 3
    assert captured["collection"] == "recursive_ingest_test"
    assert set(captured["chunk_ids"]) == {"root", "child", "descendant"}
    assert Path(result.record_path).is_file()


def test_ingest_yaml_directory_reports_empty_directory(tmp_path: Path) -> None:
    """空目录应返回可审计的 failed Record，而不是静默成功。"""
    result = ingest.ingest_yaml_directory(
        tmp_path,
        collection="recursive_ingest_test",
        record_directory=tmp_path / "records",
    )

    assert result.record.status == "failed"
    assert result.record.discovered_count == 0
    assert result.record.indexed_count == 0
    assert result.record.errors[0].stage == "discover"
    assert "没有 YAML 文件" in result.record.errors[0].message
