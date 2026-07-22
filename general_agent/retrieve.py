"""Corpus lookup for the general agent — IAM-safe, best-effort.

Two ways to reach the SLED competitive-intelligence knowledge base:

1. ``retrieve`` — a direct ``bedrock-agent-runtime.retrieve`` against the KB.
   This returns chunks *with relevance scores*, so the handler can decide whether
   the corpus actually has anything on-topic. It requires ``bedrock:Retrieve`` on
   the KB, which this Lambda's role (``sled-scoring-agent-role``) may NOT have —
   that grant lives on the docs Lambda's role — so it can raise AccessDenied.

2. ``docs_fallback`` — POST the existing ``sled-docs-query`` HTTP endpoint, which
   already has proven KB access. No scores come back (the docs Lambda runs its own
   anti-refusal generation), so the generation LLM downstream decides whether the
   answer is substantive.

``gather_corpus_context`` tries (1) then (2), returning normalized context text
plus a ``source`` tag ("retrieve" | "docs" | None) the handler maps to ``engine``.
boto3 is imported lazily so this module stays unit-testable without AWS.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import List, Optional, Tuple

_DEFAULT_KB_ID = "FIFNL0U11I"
_DEFAULT_NUM_RESULTS = 8
_DEFAULT_MIN_SCORE = 0.4

_client = None


def _agent_runtime():
    global _client
    if _client is None:
        import boto3  # lazy so tests need no AWS/boto3

        region = os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_REGION") or "us-east-1"
        _client = boto3.client("bedrock-agent-runtime", region_name=region)
    return _client


def retrieve(query: str, *, kb_id: Optional[str] = None, num_results: int = _DEFAULT_NUM_RESULTS) -> List[dict]:
    """Return the KB's retrieval results (may be empty). Raises on AccessDenied.

    Each item: {"text": str, "score": float, "source": str}. The caller decides
    relevance from the scores; we do not filter here.
    """
    kb_id = kb_id or os.environ.get("GENERAL_KB_ID", _DEFAULT_KB_ID)
    resp = _agent_runtime().retrieve(
        knowledgeBaseId=kb_id,
        retrievalQuery={"text": query},
        retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": num_results}},
    )
    results = []
    for item in resp.get("retrievalResults", []):
        text = (item.get("content") or {}).get("text", "")
        if not text:
            continue
        loc = item.get("location") or {}
        source = (
            (loc.get("s3Location") or {}).get("uri")
            or (loc.get("webLocation") or {}).get("url")
            or ""
        )
        results.append({"text": text, "score": float(item.get("score") or 0.0), "source": source})
    return results


def _format_chunks(results: List[dict], limit: int = 6) -> str:
    lines = []
    for i, r in enumerate(results[:limit], 1):
        label = os.path.basename(r["source"].rstrip("/")) if r["source"] else f"result {i}"
        lines.append(f"[Source {i}: {label}]\n{r['text'].strip()}")
    return "\n\n".join(lines)


def docs_fallback(query: str, *, docs_url: str, bearer: str = "") -> Optional[str]:
    """POST the existing sled-docs-query endpoint; return its answer text or None."""
    if not docs_url:
        return None
    data = json.dumps({"query": query}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(docs_url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None
    # The docs endpoint returns its Lambda's raw proxy envelope
    # ({"statusCode":.., "body":"<json string>"}), so the real {"response":..}
    # is one level down. Unwrap it before reading the answer.
    if isinstance(body, dict) and "response" not in body and isinstance(body.get("body"), str):
        try:
            body = json.loads(body["body"])
        except (json.JSONDecodeError, TypeError):
            pass
    if not isinstance(body, dict):
        return None
    text = body.get("response") or body.get("answer") or body.get("result")
    return text or None


def gather_corpus_context(
    query: str,
    *,
    kb_id: Optional[str] = None,
    docs_url: str = "",
    bearer: str = "",
    min_score: Optional[float] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Best-effort corpus context.

    Returns (context_text, source) where source is "retrieve", "docs", or None.
    - Tries direct KB retrieve first; if the top score clears ``min_score`` the
      formatted chunks are the context (source="retrieve"). Below threshold ->
      treat the corpus as having nothing on-topic (return no context so the
      handler answers from general knowledge).
    - On any retrieve failure (AccessDenied, throttling, etc.) falls back to the
      docs endpoint (source="docs").
    """
    if min_score is None:
        min_score = float(os.environ.get("GENERAL_RETRIEVE_MIN_SCORE", _DEFAULT_MIN_SCORE))

    try:
        results = retrieve(query, kb_id=kb_id)
    except Exception as e:  # AccessDenied is the expected one; degrade to docs endpoint
        print(json.dumps({"event": "retrieve_failed", "error": type(e).__name__, "detail": str(e)[:200]}))
        answer = docs_fallback(query, docs_url=docs_url, bearer=bearer)
        return (answer, "docs" if answer else None)

    if results and results[0]["score"] >= min_score:
        return (_format_chunks(results), "retrieve")
    # Retrieve worked but nothing is relevant -> no corpus context.
    return (None, None)
