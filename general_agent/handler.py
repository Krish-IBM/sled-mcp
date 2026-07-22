"""SLED general-purpose agent — Lambda entry point.

Answers any question the specialized agents don't cover, STRICTLY from the SLED
CI corpus (the SLED knowledge base / S3 bucket). Pulls context from the corpus;
if the corpus has nothing on-topic it says so rather than answering from the
model's own general knowledge. One synchronous Bedrock generation call; well
under the router's 29s budget.

Request  (POST, forwarded by the router): {"query": "..."}
Response: {"response": "...", "engine": "corpus|none", "corpus_source": "..."}
"""

from __future__ import annotations

import json
import os

from scoring_agent.bedrock import BedrockClient

from .retrieve import gather_corpus_context

_DEFAULT_MODEL = "us.anthropic.claude-sonnet-4-20250514-v1:0"
_MAX_TOKENS = int(os.environ.get("GENERAL_MAX_TOKENS", "2000"))

_SYSTEM_PROMPT = (
    "You are IBM's SLED (State, Local, and Education) competitive-intelligence "
    "assistant. You help IBM sellers and bid teams with questions about public-"
    "sector procurements and competitors.\n\n"
    "You answer STRICTLY from IBM's SLED competitive-intelligence corpus (the "
    "SLED knowledge base of vendor proposals, pricing, RFPs, and scoresheets). "
    "You must NOT use outside or general knowledge, and you must NOT rely on "
    "anything you know beyond the corpus material supplied below.\n\n"
    "Guidelines:\n"
    "- Answer ONLY using the SLED corpus material provided below. Ground every "
    "statement in it and cite the source document/vendor/procurement. Prefer "
    "specifics: vendor names, dollar figures, scores.\n"
    "- If no corpus material is provided, or the material does not address the "
    "question, reply that the SLED corpus does not contain information on that "
    "topic. Do NOT answer from general knowledge, and never guess or fabricate "
    "documents, vendors, scores, or contract values.\n"
    "- If the request is really a job for a specialized SLED tool, briefly point "
    "the user to it: scoring a deal / ranking bids -> the scoring agent; a "
    "competitor's bid strategy across the corpus -> the competitor-analysis agent; "
    "debriefing a won/lost deal from a recording -> the deal-debrief agent.\n"
    "- Be direct and concise."
)

_bedrock_client = None


def _bedrock() -> BedrockClient:
    global _bedrock_client
    if _bedrock_client is None:
        model = os.environ.get("GENERAL_MODEL_ID", _DEFAULT_MODEL)
        _bedrock_client = BedrockClient(strong_model=model)
    return _bedrock_client


def _extract_query(event):
    """Read {"query": ...} from a raw invoke OR an HTTP-API proxy body.

    Behind the HTTP API the payload arrives as a JSON string in event["body"];
    a direct Lambda invoke passes the dict at the root. (This is the parse the
    deal-debrief backend originally got wrong — see DEAL_DEBRIEF_BACKEND_PATCH.md.)
    """
    body = event
    if isinstance(event, dict) and "body" in event:
        try:
            body = json.loads(event["body"])
        except (json.JSONDecodeError, TypeError):
            body = event.get("body", {}) or {}
    if not isinstance(body, dict):
        body = {}
    return body.get("query") or body.get("question") or ""


def _extract_bearer(event) -> str:
    headers = (isinstance(event, dict) and event.get("headers")) or {}
    for key, value in headers.items():
        if key.lower() == "authorization" and isinstance(value, str):
            if value.lower().startswith("bearer "):
                return value[7:]
            return value
    return ""


def _generate(query: str, corpus_context) -> str:
    if corpus_context:
        user_text = (
            f"User question:\n{query}\n\n"
            f"SLED corpus material (answer ONLY from this):\n{corpus_context}"
        )
    else:
        user_text = (
            f"User question:\n{query}\n\n"
            "No SLED corpus material was found for this question. Tell the user "
            "the SLED corpus does not contain information on this topic. Do NOT "
            "answer from general knowledge."
        )
    return _bedrock().converse(user_text, system=_SYSTEM_PROMPT, max_tokens=_MAX_TOKENS)


def _response(status: int, payload: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps(payload),
    }


def lambda_handler(event, context):
    query = _extract_query(event)
    if not query.strip():
        return _response(400, {"error": "No query provided"})

    bearer = _extract_bearer(event)
    docs_url = os.environ.get("SLED_DOCS_QUERY_URL", "")

    try:
        corpus_context, source = gather_corpus_context(
            query, docs_url=docs_url, bearer=bearer
        )
    except Exception as e:  # corpus is best-effort; never fail the whole request on it
        print(json.dumps({"event": "gather_corpus_failed", "error": type(e).__name__}))
        corpus_context, source = None, None

    try:
        answer = _generate(query, corpus_context)
    except Exception as e:
        print(json.dumps({"event": "generation_failed", "error": type(e).__name__, "detail": str(e)[:300]}))
        return _response(500, {"error": f"generation failed: {type(e).__name__}"})

    engine = "corpus" if corpus_context else "none"
    return _response(200, {"response": answer, "engine": engine, "corpus_source": source or ""})
