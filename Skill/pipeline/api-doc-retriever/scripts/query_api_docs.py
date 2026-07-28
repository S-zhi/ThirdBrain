#!/usr/bin/env python3
"""Call the version-scoped local API-document query endpoint."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ENDPOINT = "/api/v1/agent/query/once"
DEFAULT_BASE_URL = "http://127.0.0.1:8000"


class QueryClientError(RuntimeError):
    """Represent a safe client, transport, or response-validation failure."""


def _top_k(value: str) -> int:
    """Parse and validate the service-supported top-k range."""
    parsed = int(value)
    if not 1 <= parsed <= 20:
        raise argparse.ArgumentTypeError("top-k must be between 1 and 20")
    return parsed


def _positive_timeout(value: str) -> float:
    """Parse and validate a positive request timeout."""
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("timeout must be greater than zero")
    return parsed


def _base_url(value: str) -> str:
    """Validate and normalize an HTTP service base URL."""
    normalized = value.rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise argparse.ArgumentTypeError("base-url must be an absolute HTTP(S) URL")
    return normalized


def _parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = argparse.ArgumentParser(
        description="Query versioned API documentation from the local Agent gateway."
    )
    parser.add_argument(
        "--query", required=True, help="API name, API ID, or intent text"
    )
    parser.add_argument(
        "--query-type",
        required=True,
        choices=("name", "semantic"),
        help="Use name for identifiers and semantic for natural-language intent",
    )
    parser.add_argument(
        "--namespace", required=True, help="Complete versioned namespace"
    )
    parser.add_argument("--version", required=True, help="Exact version segment")
    parser.add_argument("--language", help="Optional language filter, such as cpp")
    parser.add_argument(
        "--top-k", type=_top_k, default=5, help="Number of results, 1-20"
    )
    parser.add_argument(
        "--base-url",
        type=_base_url,
        default=_base_url(os.environ.get("COLD_API_BASE_URL", DEFAULT_BASE_URL)),
        help="Service base URL; defaults to COLD_API_BASE_URL or localhost:8000",
    )
    parser.add_argument(
        "--timeout",
        type=_positive_timeout,
        default=30.0,
        help="Request timeout in seconds",
    )
    parser.add_argument(
        "--pretty", action="store_true", help="Pretty-print JSON output"
    )
    return parser


def _payload(args: argparse.Namespace) -> dict[str, Any]:
    """Build the gateway request payload from validated arguments."""
    filters: dict[str, str] = {
        "namespace": args.namespace.strip(),
        "version": args.version.strip(),
    }
    if args.language:
        filters["language"] = args.language.strip()
    return {
        "query": args.query.strip(),
        "query_type": args.query_type,
        "top_k": args.top_k,
        "filters": filters,
    }


def _decode_json(raw: bytes, *, context: str) -> dict[str, Any]:
    """Decode a JSON object or raise a safe response error."""
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QueryClientError(f"{context} did not contain valid UTF-8 JSON") from error
    if not isinstance(decoded, dict):
        raise QueryClientError(f"{context} JSON must be an object")
    return decoded


def _query(base_url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    """Send one query and return the validated response object."""
    request = Request(
        f"{base_url}{ENDPOINT}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            result = _decode_json(response.read(), context="service response")
    except HTTPError as error:
        body = error.read()
        try:
            detail: object = _decode_json(body, context="error response")
        except QueryClientError:
            detail = body.decode("utf-8", errors="replace") or error.reason
        raise QueryClientError(
            f"service returned HTTP {error.code}: "
            f"{json.dumps(detail, ensure_ascii=False)}"
        ) from error
    except URLError as error:
        raise QueryClientError(f"cannot reach query service: {error.reason}") from error

    documents = result.get("documents")
    total = result.get("total")
    if not isinstance(documents, list) or not isinstance(total, int):
        raise QueryClientError("service response is missing documents or total")
    return result


def main() -> int:
    """Parse arguments, execute the query, and print machine-readable JSON."""
    args = _parser().parse_args()
    payload = _payload(args)
    if (
        not payload["query"]
        or not payload["filters"]["namespace"]
        or not payload["filters"]["version"]
    ):
        print("query, namespace, and version must not be blank", file=sys.stderr)
        return 2
    try:
        result = _query(args.base_url, payload, args.timeout)
    except QueryClientError as error:
        print(str(error), file=sys.stderr)
        return 1

    indent = 2 if args.pretty else None
    print(json.dumps(result, ensure_ascii=False, indent=indent))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
