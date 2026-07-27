"""端到端冒烟测试：拿 2 份真实 YAML → 走 ORM→Doc→Embedder→Zvec→Search 全流程。

只用于验证 pipeline 跑通，不替代单测。运行前会临时切到 local embedder
（避免依赖 DASHSCOPE_API_KEY）。
"""

import os
import shutil
import sys
import time
import yaml
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from src.dao.emb import (  # noqa: E402
    ApiDocumentLike,
    build_embedder,
    from_orm,
    open_or_create_collection,
    upsert_batch,
    search,
    search_by_name,
    wait_for_index_ready,
    SearchQuery,
)


# ---------------------------------------------------------------------------
# 临时 config：切到 local embedder + 临时 collection 路径
# ---------------------------------------------------------------------------

TMP_COLL_PATH = ROOT / "tmp" / "e2e_zvec_data"
TEST_COLL_NAME = "e2e_test"

if TMP_COLL_PATH.exists():
    shutil.rmtree(TMP_COLL_PATH)
TMP_COLL_PATH.mkdir(parents=True, exist_ok=True)

from dataclasses import replace

config.reset_config()
cfg = config.load_config()
new_zvec = replace(cfg.zvec, collection_path=str(TMP_COLL_PATH), default_collection=TEST_COLL_NAME)
new_emb = replace(cfg.embedder, type="local")  # 切到 local 跑测试
config._config = replace(cfg, zvec=new_zvec, embedder=new_emb)


# ---------------------------------------------------------------------------
# 把真实 YAML 转成 ApiDocumentLike
# ---------------------------------------------------------------------------

@dataclass
class YamlDoc(ApiDocumentLike):
    """满足 ApiDocumentLike 形状的最小 ORM 占位。"""
    chunk_id: str = ""
    name: str = ""
    namespace: str = ""
    language: str = ""
    category: str = ""
    title: str = ""
    description: str = ""
    params_md: str = ""
    returns: str = ""
    examples: list = field(default_factory=list)
    body_md: str = ""
    product_support: list = field(default_factory=list)
    signature: str = ""
    deprecated: bool = False
    deprecation_note: str = ""
    ingested_at: int = 0


def load_yaml_as_orm(path: Path) -> YamlDoc:
    with path.open(encoding="utf-8") as f:
        d = yaml.safe_load(f)
    return YamlDoc(
        chunk_id=d["chunk_id"],
        name=d["name"],
        namespace=d["namespace"],
        language=d["language"],
        category=d["category"],
        title=d.get("title", ""),
        description=d.get("description", ""),
        params_md=d.get("params_md", ""),
        returns=d.get("returns", ""),
        examples=d.get("examples", []) or [],
        body_md=d.get("body_md", ""),
        product_support=d.get("product_support", []) or [],
        signature=d.get("signature", ""),
        deprecated=d.get("deprecated", False),
        deprecation_note=d.get("deprecation_note", ""),
        ingested_at=int(time.time()),
    )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    # 1. 加载 2 份真实 YAML
    yaml_dir = ROOT / "ingest" / "output" / "yaml" / "AI_CPU_API"
    yaml_files = [
        yaml_dir / "DataStoreBarrier.yaml",
        yaml_dir / "assert.yaml",
    ]
    print(f"[1/6] Loading {len(yaml_files)} YAML files...")
    orm_docs = [load_yaml_as_orm(p) for p in yaml_files]
    for d in orm_docs:
        print(f"      - {d.chunk_id} ({d.name})")

    # 2. ORM → zvec.Doc
    print("\n[2/6] ORM → zvec.Doc...")
    zvec_docs = [from_orm(d) for d in orm_docs]
    for d in zvec_docs:
        print(f"      - {d.id} fields={len(d.fields)}")

    # 3. Build embedder (local)
    print("\n[3/6] Building local embedder (first call downloads model)...")
    t0 = time.time()
    embedder = build_embedder()
    print(f"      - embedder type: {type(embedder).__name__}, init took {time.time()-t0:.1f}s")

    # 3.5 Fit sparse encoder on the corpus（不然 sparse 召回全空）
    corpus = [d.description + " " + d.name for d in orm_docs]
    embedder.fit_sparse(corpus)
    print(f"      - fit_sparse done, n_docs={embedder.sparse_encoder.n_docs}")

    # 4. Open/create collection
    print("\n[4/6] Open/create collection...")
    coll = open_or_create_collection(TEST_COLL_NAME)
    print(f"      - path: {coll.stats}")

    # 5. Upsert batch
    print("\n[5/6] Upsert batch (2 docs, embed + write)...")
    t0 = time.time()
    result = upsert_batch(coll, zvec_docs, embedder)
    print(f"      - result: {result}, took {time.time()-t0:.1f}s")
    print(f"      - stats: {coll.stats}")

    # Optimize + wait
    coll.optimize()
    print("      - optimize() called, waiting for index ready...")
    ready = wait_for_index_ready(coll, timeout=60)
    print(f"      - index ready: {ready}, stats: {coll.stats}")
    assert ready, "index didn't become ready in 60s"

    # 6. Search tests
    print("\n[6/6] Search tests...")

    # Test 1: exact name match (short-circuit)
    print("\n  Test 1: search_by_name('DataStoreBarrier')")
    results = search_by_name(coll, "DataStoreBarrier", topk=5)
    for r in results:
        print(f"    [{r.score:.4f}] {r.doc_id} :: {r.fields.get('api_name', '')[:40]}")
    assert any(r.doc_id == "com.huawei.cann.ascendc.op.910beta3.datastorebarrier" for r in results), \
        "expected DataStoreBarrier to be in results"

    # Test 2: full search with filter (namespace)
    print("\n  Test 2: search '数据同步' (semantic, namespace filtered)")
    q = SearchQuery(
        text="数据同步",
        namespace="com.huawei.cann.ascendc.op.910beta3",
        topk=5,
    )
    results = search(coll, q, embedder)
    for r in results:
        print(f"    [{r.score:.4f}] {r.doc_id} :: {r.fields.get('api_name', '')[:50]}")
    assert results, "expected non-empty results"

    # Test 3: search with deprecated filter
    print("\n  Test 3: search 'assert' (should find assert.yaml)")
    q = SearchQuery(text="assert condition check", topk=3)
    results = search(coll, q, embedder)
    for r in results:
        print(f"    [{r.score:.4f}] {r.doc_id} :: {r.fields.get('name', '')}")
    assert any("assert" in r.doc_id for r in results), "expected assert in results"

    # Test 4: filter out deprecated
    print("\n  Test 4: search with language filter (cpp)")
    q = SearchQuery(text="memory barrier", language="cpp", topk=3)
    results = search(coll, q, embedder)
    for r in results:
        print(f"    [{r.score:.4f}] {r.doc_id} :: {r.fields.get('name', '')}")

    print("\n✅ End-to-end smoke test PASSED")
    embedder.close()


if __name__ == "__main__":
    main()
