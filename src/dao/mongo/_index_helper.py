"""MongoDB index utilities to ensure identical configurations and drift detection."""

from __future__ import annotations

import logging
from typing import Any

from pymongo.errors import PyMongoError

from src.dao.mongo._tracing import remap_pymongo_error

logger = logging.getLogger(__name__)


def _index_keys_match(a: Any, b: Any) -> bool:
    """Compare whether two index key descriptions are identical.

    Handles different pymongo formats: list of tuples or dict/SON.
    """
    if a is None or b is None:
        return a == b
    a_pairs = [(k, v) for k, v in (a.items() if isinstance(a, dict) else a)]
    b_pairs = [(k, v) for k, v in (b.items() if isinstance(b, dict) else b)]
    return a_pairs == b_pairs


async def create_index_if_missing(
    collection: Any,
    keys: list[tuple[str, int]],
    *,
    name: str,
    partial_filter: dict[str, Any] | None = None,
) -> None:
    """Idempotently create an index; skip if already exists; raise on drift.

    Args:
        collection: The Motor/PyMongo collection.
        keys: List of (field, direction) pairs.
        name: Name of the index.
        partial_filter: Optional partialFilterExpression.

    Raises:
        RuntimeError: If key or partialFilterExpression drift is detected.
        DAOError: Re-mapped MongoDB errors.
    """
    existing_iter = await collection.list_indexes()
    existing = {idx["name"]: idx async for idx in existing_iter}

    if name in existing:
        existing_idx = existing[name]
        existing_key = existing_idx.get("key")
        if not _index_keys_match(existing_key, keys):
            raise RuntimeError(
                f"index {collection.name}.{name} key drift: "
                f"existing={existing_key}, expected={keys}; "
                f"manual migration required"
            )

        existing_filter = existing_idx.get("partialFilterExpression")
        if partial_filter != existing_filter:
            raise RuntimeError(
                f"index {collection.name}.{name} partialFilterExpression drift: "
                f"existing={existing_filter}, expected={partial_filter}; "
                f"manual migration required"
            )
        return

    try:
        options: dict[str, Any] = {"name": name}
        if partial_filter is not None:
            options["partialFilterExpression"] = partial_filter
        await collection.create_index(keys, **options)
    except PyMongoError as exc:
        msg = str(exc)
        if "already exists" in msg or "IndexOptionsConflict" in msg or "IndexKeySpecsConflict" in msg:
            # Race condition: another process created the index concurrently.
            return
        raise remap_pymongo_error(exc) from exc
