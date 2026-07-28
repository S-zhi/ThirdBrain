---
name: api-doc-retriever
description: Query the local RAG With Cold API Documents service for version-scoped API contracts and examples. Use when an agent must generate, review, debug, or explain code that calls an unfamiliar, private, or version-sensitive API; when a user asks to look up an API by name or intent; or when API signatures, parameters, return values, constraints, deprecations, and examples must be verified before coding.
---

# API Document Retriever

Retrieve authoritative API context from the project's local query service before reasoning about an unfamiliar or version-sensitive API. Treat the returned document fields as evidence; do not fill gaps from memory.

## Required scope

Obtain both values before querying:

- `namespace`: the complete dotted namespace with authoritative casing, including its version segment.
- `version`: the exact version segment.

Take them only from explicit user input, repository configuration, imports, lockfiles, or already retrieved documents. Never invent either value or replace it with `latest`, `unknown`, or a cross-namespace search. If either remains unknown after inspecting the task context, ask the user for it.

## Query workflow

1. Locate this Skill directory and use `scripts/query_api_docs.py` from it.
2. Select `name` for an API identifier or fully qualified API ID. Select `semantic` for a natural-language capability or behavior.
3. Run one scoped query:

   ```bash
   python3 scripts/query_api_docs.py \
     --query 'DataStoreBarrier' \
     --query-type name \
     --namespace 'com.huawei.cann.ascendc.op.910beta3' \
     --version '910beta3' \
     --language cpp \
     --pretty
   ```

4. Read `total` and `documents`. Verify every selected document has the requested `namespace` and `version`.
5. Ground code or advice in `signature`, `parameters_md`, `returns_json`, `examples`, `description`, `deprecation_note`, and `source_markdown`. Preserve exact identifiers and argument order.
6. State which `api_id` and version support the result when reporting the answer.

The client uses `http://127.0.0.1:8000` by default. Pass `--base-url` or set `COLD_API_BASE_URL` when the service runs elsewhere. Use `--help` for all arguments.

## Handling uncertain results

- If an exact-name query returns zero documents, retry once with the fully qualified API ID when it is known; otherwise use one semantic query with the same scope.
- If a semantic query returns several plausible documents, compare their signatures, contracts, return values, and examples. Do not silently choose by score alone.
- If all scoped attempts return zero documents, report that the index has no verified match. Do not widen namespace or version automatically.
- Treat HTTP `422` as an invalid request, HTTP `503` as a retrieval-backend failure, and a connection error as a stopped or unreachable local service. Surface the structured error instead of guessing an API answer.
- `record_status: failed` means MongoDB trace persistence failed; retrieved documents are still usable when the HTTP request succeeded.

## Service prerequisite

When working in the RAG With Cold API Documents repository and the service is not running, start it from the repository root only when local process startup is within the user's request:

```bash
uv run uvicorn src.main:app --host 127.0.0.1 --port 8000
```

The service requires its configured MongoDB and Zvec collection. A successful HTTP response with `total > 0` is the end-to-end proof that the query path is usable.
