# ThirdBrain PR review rules

Review the pull request against the repository's `AGENTS.md` and the rules below. Focus on
high-confidence correctness, security, data-isolation, and regression risks. Do not block for
purely stylistic preferences.

## Blocking findings

- Retrieval, ingestion, or persistence code can mix API documents across namespace, product, or
  version boundaries, or silently rewrites official namespace/version identifiers.
- A public service endpoint is added without the repository's authentication middleware, or a
  change exposes secrets, credentials, private API documents, or unsanitized benchmark data.
- A schema or context-package change drops parameter contracts, call constraints, deprecation
  state, provenance, or other machine-consumable fields without an explicit migration path.
- A retrieval strategy can return an unversioned result where a version is required, or bypasses
  namespace filtering for exact-name or fallback searches.
- A change can corrupt or irreversibly lose ingestion/index data during partial failure, retry,
  cancellation, or concurrent execution.
- New behavior has no focused unit test, or a new retrieval strategy has no benchmark case.

## Warnings

- The five trace stages (`trigger`, `recall`, `rerank`, `inject`, `generate`) become incomplete,
  inconsistent, or harder to attribute.
- Async request paths perform blocking network, model, database, or vector-index work directly on
  the event loop.
- Error handling hides actionable context, turns partial failures into success, or makes retries
  non-idempotent.
- Public Python APIs lack precise typing compatible with strict mypy, or Pydantic v2 models are
  bypassed at trust boundaries.
- Configuration, secrets, timeouts, or provider-specific values are hard-coded instead of using
  the existing settings/configuration layer.

## Review scope

- Pay particular attention to `src/dao/emb/`, `src/knowledge/`, `src/retrieve/`, `src/gateway/`,
  ingestion/sync scripts, schemas, benchmark cases, and service authentication.
- Treat generated files, lockfiles, and documentation-only wording changes as low priority unless
  they introduce a security or operational problem.
- Cite the affected file and line whenever possible, explain the failure scenario, and suggest the
  smallest safe fix.
