"""
MCP Server Lambda — SLED Competitive Intelligence
Resource server: validates w3id JWT, serves MCP over stateless HTTP.
"""

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import base64
import hashlib
import time
import uuid
from typing import Optional

import jwt
from jwt import PyJWKClient

# Load .env for local dev before any os.environ reads below. No-op in Lambda,
# where config comes from the function's own environment variables.
try:
    import env_config  # noqa: F401
except Exception:
    pass

# ── w3id verifier (env vars set in Lambda config) ─────────────────────────────
ISSUER   = os.environ.get("W3ID_ISSUER",   "https://login.w3.ibm.com/oidc/endpoint/default")
JWKS_URI = os.environ.get("W3ID_JWKS_URI", "https://login.w3.ibm.com/v1.0/endpoint/default/jwks")
AUDIENCE = os.environ["W3ID_AUDIENCE"]     # full w3id client_id
W3ID_CLIENT_ID = os.environ.get("W3ID_CLIENT_ID", AUDIENCE)
W3ID_CLIENT_SECRET_ARN = os.environ.get("W3ID_CLIENT_SECRET_ARN", "")
W3ID_INTROSPECTION_URL = os.environ.get(
    "W3ID_INTROSPECTION_URL",
    "https://login.w3.ibm.com/v1.0/endpoint/default/introspect",
)

_jwks = PyJWKClient(JWKS_URI)              # module-level: reused across warm invocations
_client_secret_cache = None

# ── Introspection result cache ────────────────────────────────────────────────
# w3id tokens are opaque, so verify_token falls back to a POST introspection call
# (up to 8s) on EVERY authenticated request — initialize, tools/list, each
# tools/call. Under Otto's re-handshake churn that adds up and can look like the
# connector "timing out". Cache successful introspections per token (module-level,
# reused across warm invocations) for a short TTL, never past the token's own exp.
# Trade-off: a token revoked mid-TTL stays accepted until the entry expires, so
# keep the TTL modest (default 300s; tune via INTROSPECTION_CACHE_TTL=0 to disable).
_introspection_cache = {}                  # sha256(token) -> (expiry_epoch, claims)
_INTROSPECTION_CACHE_TTL = int(os.environ.get("INTROSPECTION_CACHE_TTL", "300"))
_INTROSPECTION_CACHE_MAX = int(os.environ.get("INTROSPECTION_CACHE_MAX", "1000"))


def _introspection_cache_get(token_hash: str):
    entry = _introspection_cache.get(token_hash)
    if not entry:
        return None
    expiry, claims = entry
    if expiry <= time.time():
        _introspection_cache.pop(token_hash, None)
        return None
    return claims


def _introspection_cache_put(token_hash: str, claims: dict) -> None:
    if _INTROSPECTION_CACHE_TTL <= 0:
        return
    now = time.time()
    if len(_introspection_cache) >= _INTROSPECTION_CACHE_MAX:
        for stale in [k for k, (exp, _) in _introspection_cache.items() if exp <= now]:
            _introspection_cache.pop(stale, None)
        if len(_introspection_cache) >= _INTROSPECTION_CACHE_MAX:
            _introspection_cache.clear()   # simple bound; cold-ish restart of the cache
    expiry = now + _INTROSPECTION_CACHE_TTL
    token_exp = claims.get("exp")
    if token_exp is not None:
        try:
            expiry = min(expiry, int(token_exp))
        except (TypeError, ValueError):
            pass
    _introspection_cache[token_hash] = (expiry, claims)

# ── Backend URLs ──────────────────────────────────────────────────────────────
SLED_DOCS_QUERY_URL = os.environ["SLED_DOCS_QUERY_URL"]
ANALYZE_DEAL_URL    = os.environ.get("ANALYZE_DEAL_URL", "")
SCORING_AGENT_URL   = os.environ.get("SCORING_AGENT_URL", "")
BID_ANALYSIS_AGENT_URL = os.environ.get(
    "BID_ANALYSIS_AGENT_URL",
    "https://5uanpy2351.execute-api.us-east-1.amazonaws.com/",
)
COMPETITOR_ANALYSIS_URL = os.environ.get("COMPETITOR_ANALYSIS_URL", "")

# ── Agent registry — maps agent name → backend URL, payload key, description ───
# payload_key is the JSON field each backend expects (docs/scoring use "query";
# the deal backend was built for "question").
# keywords drive server-side auto-selection when the caller does not name an
# agent (see choose_agent). description is surfaced to the client as the per-agent
# tool description, so keep it specific enough for an LLM to pick correctly.
# To add a new agent: add an entry here and set the corresponding env var in Lambda.
AGENT_REGISTRY = {
    "scoring": {
        "url": SCORING_AGENT_URL,
        "payload_key": "query",
        "description": (
            "Score competing vendor bids/proposals on a government procurement: "
            "RFP scorecard, vendor ranking, and competitive intelligence vs. IBM. "
            "Use for scoring a deal, ranking vendors, evaluating proposals, or "
            "generating a scorecard."
        ),
        "keywords": (
            "score", "scoring", "scorecard", "bid", "bids", "proposal", "proposals",
            "rank", "ranking", "rfp", "evaluate", "evaluation", "vendor", "vendors",
            "pricing",
        ),
    },
    "docs": {
        "url": SLED_DOCS_QUERY_URL,
        "payload_key": "query",
        "description": (
            "Query the SLED competitive-intelligence knowledge base. Use for "
            "questions about competitors, market/customer references, incumbents, "
            "contract history, or general SLED background."
        ),
        "keywords": (
            "competitor", "competitors", "competitive", "market", "customer",
            "customers", "reference", "references", "incumbent", "incumbents",
            "contract", "contracts", "knowledge", "who", "where", "which", "history",
        ),
    },
    "deal": {
        "url": ANALYZE_DEAL_URL,
        "payload_key": "question",
        "description": (
            "Analyze a single deal/opportunity in depth. Use for assessing one "
            "opportunity or a focused deal analysis."
        ),
        "keywords": (
            "analyze", "analyse", "analysis", "opportunity", "opportunities",
            "assess", "assessment", "deal", "pipeline",
        ),
    },
    "bid_analysis": {
        "url": BID_ANALYSIS_AGENT_URL,
        "payload_key": "query",
        "description": (
            "Analyze solicitation, RFP, RFQ, proposal, and bid materials to produce "
            "opportunity summaries, compliance requirements, deadlines, evaluation "
            "criteria, risks, pursuit guidance, and IBM-relevant bid intelligence."
        ),
        "keywords": (
            "bid", "proposal", "solicitation", "rfp", "rfq", "compliance",
            "requirements", "no", "no-bid", "pursuit", "opportunity", "analysis",
            "analyze", "deadlines", "deadline", "forms", "red", "flags",
        ),
    },
    "competitor_analysis": {
        "url": COMPETITOR_ANALYSIS_URL,
        "payload_key": "query",
        "description": (
            "Analyze one competitor's bid strategy across ALL their FOIA documents "
            "in the corpus: solutioning/technical approach, staffing, pricing, past "
            "performance, and win themes — with implications for IBM. Async: "
            "'analyze competitor=\"<name>\"' returns a job_id; then 'status <id>' / "
            "'result <id>' for the report (Word + JSON downloads). 'competitors' "
            "lists available vendors."
        ),
        "keywords": (
            "competitor", "strategy", "strategies", "solutioning", "staffing",
            "playbook", "differentiators", "themes", "profile", "positioning",
            "analyze", "analysis", "pricing", "win",
        ),
    },
}

# Agent used when the keyword heuristic finds no match. docs is always configured
# (SLED_DOCS_QUERY_URL is required), so it is a safe default; override via env.
DEFAULT_AGENT = os.environ.get("DEFAULT_AGENT", "docs")

# Per-agent tool name → agent name (e.g. "sled_scoring" → "scoring").
_AGENT_TOOL_NAMES = {f"sled_{name}": name for name in AGENT_REGISTRY}

# ── MCP server identity ───────────────────────────────────────────────────────
MCP_SERVER_NAME    = "sled-competitive-intel"
MCP_SERVER_VERSION = "2.0.0"
MCP_PROTOCOL_VERSION = "2025-11-25"
MCP_SUPPORTED_PROTOCOL_VERSIONS = {
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
}
_EXPOSE_HEADERS = "WWW-Authenticate, MCP-Session-Id, MCP-Protocol-Version, Allow"

# When enabled (MCP_STATEFUL_SESSIONS=1), the server returns an MCP-Session-Id on
# initialize and echoes it on later responses. Default OFF (stateless, POST-only).
# This is an experiment to stop Otto's client-side initialize-only reconnect loop:
# some MCP clients treat a session-id-less initialize as "not established" and
# re-handshake forever without re-issuing tools/list, so the tool disappears from
# the client's registry. We still NEVER open a GET SSE stream — we only mint an
# opaque session id (no server-side state). Enable on a TEST connector first and
# watch for tools/list following initialize (see OTTO_MCP_DIAGNOSIS.md).
MCP_STATEFUL_SESSIONS = os.environ.get("MCP_STATEFUL_SESSIONS", "").strip().lower() in (
    "1", "true", "yes", "on",
)

# ── Request helpers (REST API v1, HTTP API v2, and Lambda Function URL) ───────
# Fallback env vars used only when headers are absent (direct Lambda invoke).
_FALLBACK_HOST  = os.environ.get("API_HOST", "zie08z9fuj.execute-api.us-east-1.amazonaws.com")
_FALLBACK_STAGE = os.environ.get("STAGE", "prod")
_PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")


def _headers(event: dict) -> dict:
    return event.get("headers") or {}


def _header_value(headers: dict, name: str, default: str = "") -> str:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value or default
    return default


def _accepts(headers: dict, media_type: str) -> bool:
    return media_type.lower() in _header_value(headers, "accept").lower()


def _method(event: dict) -> str:
    method = event.get("httpMethod")
    if method:
        return method
    return ((event.get("requestContext") or {}).get("http") or {}).get("method", "")


def _path(event: dict) -> str:
    return event.get("path") or event.get("rawPath") or ""


def _base_url(event: dict) -> str:
    """Return https://host[/stage] — no prefix for HTTP API $default stage."""
    if _PUBLIC_BASE_URL:
        return _PUBLIC_BASE_URL

    headers = _headers(event)
    host  = headers.get("Host") or headers.get("host") or _FALLBACK_HOST
    stage = (event.get("requestContext") or {}).get("stage") or _FALLBACK_STAGE
    if stage and stage != "$default":
        return f"https://{host}/{stage}"
    return f"https://{host}"


def _body(event: dict) -> str:
    body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        return base64.b64decode(body).decode("utf-8")
    return body


def _configured_scopes() -> list:
    raw = os.environ.get("W3ID_SCOPES_SUPPORTED", "openid")
    return [scope for scope in raw.replace(",", " ").split() if scope]


def _log(event_name: str, **fields):
    print(json.dumps({"event": event_name, **fields}, default=str))


def _token_summary(token: str) -> dict:
    summary = {
        "token_len": len(token),
        "token_dot_count": token.count("."),
    }

    try:
        header = jwt.get_unverified_header(token)
        summary["alg"] = header.get("alg")
        summary["kid"] = header.get("kid")
    except Exception as e:
        summary["header_error"] = type(e).__name__

    try:
        claims = jwt.decode(
            token,
            options={
                "verify_signature": False,
                "verify_exp": False,
                "verify_aud": False,
                "verify_iss": False,
            },
        )
        summary["iss"] = claims.get("iss")
        summary["aud"] = claims.get("aud")
        summary["scope"] = claims.get("scope")
        summary["exp"] = claims.get("exp")
        summary["iat"] = claims.get("iat")
    except Exception as e:
        summary["claims_error"] = type(e).__name__

    return summary


def _prm(base: str) -> dict:
    # authorization_servers points to OUR server so Otto fetches AS metadata
    # from us (never touches w3id directly — w3id blocks automated fetches).
    return {
        "resource": f"{base}/mcp",
        "authorization_servers": [base],
        "scopes_supported": _configured_scopes(),
        "bearer_methods_supported": ["header"],
    }


def _as_metadata(base: str) -> dict:
    # We serve w3id's real endpoints under our issuer identifier.
    # Otto uses authorize/token to talk to w3id; our Lambda validates the JWT.
    return {
        "issuer": base,
        "authorization_endpoint": "https://login.w3.ibm.com/v1.0/endpoint/default/authorize",
        "token_endpoint": "https://login.w3.ibm.com/v1.0/endpoint/default/token",
        "introspection_endpoint": W3ID_INTROSPECTION_URL,
        "jwks_uri": "https://login.w3.ibm.com/v1.0/endpoint/default/jwks",
        "userinfo_endpoint": "https://login.w3.ibm.com/v1.0/endpoint/default/userinfo",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
        "scopes_supported": _configured_scopes(),
    }

# ── MCP tool definitions ──────────────────────────────────────────────────────
def _query_schema(description: str) -> dict:
    return {
        "type": "object",
        "properties": {"query": {"type": "string", "description": description}},
        "required": ["query"],
    }


def _build_tools() -> list:
    """One tool per configured agent, plus a router tool that auto-selects.

    Agents whose backend URL is not configured are omitted so the client never
    sees a tool it cannot use. The router tool (sled_agent) is always present.
    """
    tools = []
    for name, agent in AGENT_REGISTRY.items():
        if not agent.get("url"):
            continue
        tools.append({
            "name": f"sled_{name}",
            "description": agent["description"],
            "inputSchema": _query_schema(f"The request to send to the SLED {name} agent."),
        })

    available = ", ".join(name for name, agent in AGENT_REGISTRY.items() if agent.get("url"))
    tools.append({
        "name": "sled_agent",
        "description": (
            "Route a SLED query to the best agent automatically. Use this when you "
            f"are unsure which agent fits — the server analyzes the query and picks "
            f"one of: {available}. You may force a specific agent by starting the "
            "query with 'agent_name:' (e.g. 'scoring: score deal=\"City of Austin/ERP\"')."
        ),
        "inputSchema": _query_schema(
            "Your question. Optionally prefix with 'agent_name:' to force a specific agent."
        ),
    })
    return tools


TOOLS = _build_tools()

# ── Token verification ────────────────────────────────────────────────────────

def _get_w3id_client_secret() -> str:
    global _client_secret_cache

    if _client_secret_cache:
        return _client_secret_cache

    if not W3ID_CLIENT_SECRET_ARN:
        raise RuntimeError("w3id_client_secret_arn_not_configured")

    import boto3

    secret_value = boto3.client("secretsmanager").get_secret_value(
        SecretId=W3ID_CLIENT_SECRET_ARN
    )
    raw_secret = secret_value.get("SecretString")
    if raw_secret is None:
        raw_secret = base64.b64decode(secret_value["SecretBinary"]).decode("utf-8")

    try:
        parsed = json.loads(raw_secret)
    except json.JSONDecodeError:
        parsed = raw_secret

    if isinstance(parsed, dict):
        secret = (
            parsed.get("client_secret") or
            parsed.get("W3ID_CLIENT_SECRET") or
            parsed.get("secret")
        )
    else:
        secret = parsed

    if not secret:
        raise RuntimeError("w3id_client_secret_missing")

    _client_secret_cache = secret
    return secret


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _validate_common_claims(claims: dict) -> None:
    exp = claims.get("exp")
    if exp is not None and int(exp) <= int(time.time()):
        raise jwt.ExpiredSignatureError("Token expired")

    iss = claims.get("iss")
    if iss and iss != ISSUER:
        raise jwt.InvalidIssuerError("Invalid issuer")

    expected_client = W3ID_CLIENT_ID or AUDIENCE
    client_ids = _as_list(claims.get("client_id"))
    audiences = _as_list(claims.get("aud"))

    if client_ids and expected_client not in client_ids:
        raise jwt.InvalidAudienceError("Invalid client_id")

    if audiences and expected_client not in audiences:
        raise jwt.InvalidAudienceError("Invalid audience")


def _introspect_token(token: str) -> dict:
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    cached = _introspection_cache_get(token_hash)
    if cached is not None:
        _log("auth", status="introspection_cache_hit")
        return cached

    form = urllib.parse.urlencode({
        "token": token,
        "token_type_hint": "access_token",
        "client_id": W3ID_CLIENT_ID,
        "client_secret": _get_w3id_client_secret(),
    }).encode("utf-8")

    req = urllib.request.Request(
        W3ID_INTROSPECTION_URL,
        data=form,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            claims = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        _log("auth", status="introspection_http_error", code=e.code)
        raise jwt.InvalidTokenError("Introspection failed")

    active = claims.get("active")
    if active is not True and str(active).lower() != "true":
        raise jwt.InvalidTokenError("Inactive token")

    _validate_common_claims(claims)
    _introspection_cache_put(token_hash, claims)
    return claims

def verify_token(token: str) -> dict:
    try:
        key = _jwks.get_signing_key_from_jwt(token).key
        return jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            issuer=ISSUER,
            audience=AUDIENCE,
            options={"require": ["exp", "iat", "iss", "aud"]},
        )
    except jwt.ExpiredSignatureError:
        raise
    except Exception as e:
        if not W3ID_CLIENT_SECRET_ARN:
            raise

        _log("auth", status="jwt_fallback_to_introspection", jwt_error=type(e).__name__)
        return _introspect_token(token)


def extract_bearer(event: dict):
    headers = _headers(event)
    auth = headers.get("Authorization") or headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:]
    return None


def unauthorized_response(base: str) -> dict:
    prm_url = f"{base}/.well-known/oauth-protected-resource"
    return {
        "statusCode": 401,
        "headers": {
            "Content-Type": "application/json",
            "WWW-Authenticate": f'Bearer realm="mcp", resource_metadata="{prm_url}"',
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Expose-Headers": "WWW-Authenticate",
        },
        "body": json.dumps({"error": "unauthorized"}),
    }

# ── MCP handlers ──────────────────────────────────────────────────────────────

def handle_initialize(params: dict, _claims: dict) -> dict:
    requested_version = params.get("protocolVersion")
    protocol_version = (
        requested_version
        if requested_version in MCP_SUPPORTED_PROTOCOL_VERSIONS
        else MCP_PROTOCOL_VERSION
    )

    return {
        "protocolVersion": protocol_version,
        "serverInfo": {
            "name": MCP_SERVER_NAME,
            "title": "SLED Competitive Intelligence",
            "version": MCP_SERVER_VERSION,
            "description": "SLED competitive intelligence tools backed by IBM w3id SSO.",
        },
        "capabilities": {
            "tools": {},
        },
        "instructions": "Use the available tools to query SLED competitive intelligence documents.",
    }


def sse_not_supported_response() -> dict:
    return {
        "statusCode": 405,
        "headers": {
            "Allow": "POST, OPTIONS",
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Expose-Headers": _EXPOSE_HEADERS,
        },
        "body": json.dumps({"error": "sse_not_supported"}),
    }


def health_response() -> dict:
    return {
        "server": MCP_SERVER_NAME,
        "version": MCP_SERVER_VERSION,
        "status": "ok",
        "transport": "streamable-http",
    }


def handle_tools_list(_params: dict, _claims: dict) -> dict:
    return {"tools": TOOLS}


def handle_resources_list(_params: dict, _claims: dict) -> dict:
    return {"resources": []}


def handle_prompts_list(_params: dict, _claims: dict) -> dict:
    return {"prompts": []}


def parse_agent_prefix(query: str):
    """Return (agent_name, remaining_query) or (None, query) if prefix absent/unknown."""
    if ":" in query:
        candidate, rest = query.split(":", 1)
        name = candidate.strip().lower()
        if name in AGENT_REGISTRY:
            return name, rest.strip()
    return None, query


def _configured_agents() -> list:
    return [name for name, agent in AGENT_REGISTRY.items() if agent.get("url")]


def choose_agent(query: str) -> str:
    """Keyword-heuristic agent selection for queries with no explicit prefix.

    Tokenizes the query and scores it against each configured agent's keyword
    set, returning the best match. Ties break toward the earliest agent in the
    registry (scoring > docs > deal). Falls back to DEFAULT_AGENT — or, if that
    is unconfigured, the first agent that has a backend URL.
    """
    tokens = set(re.findall(r"[a-z0-9]+", query.lower()))

    best_agent, best_score = None, 0
    for name, agent in AGENT_REGISTRY.items():
        if not agent.get("url"):
            continue
        score = sum(1 for kw in agent.get("keywords", ()) if kw in tokens)
        if score > best_score:
            best_agent, best_score = name, score

    if best_agent:
        return best_agent
    if AGENT_REGISTRY.get(DEFAULT_AGENT, {}).get("url"):
        return DEFAULT_AGENT
    configured = _configured_agents()
    return configured[0] if configured else DEFAULT_AGENT


def _route_to_agent(agent_name: str, query_text: str, bearer_token: str) -> dict:
    agent = AGENT_REGISTRY.get(agent_name)
    if not agent:
        available = ", ".join(_configured_agents())
        return {"isError": True, "content": [{"type": "text", "text": (
            f"Unknown agent '{agent_name}'. Available: {available}."
        )}]}
    if not agent.get("url"):
        return {"isError": True, "content": [{"type": "text", "text": (
            f"Agent '{agent_name}' backend URL not configured."
        )}]}
    return _call_backend(agent["url"], {agent["payload_key"]: query_text}, bearer_token)


def handle_tools_call(params: dict, _claims: dict, bearer_token: str) -> dict:
    name = params.get("name")
    args = params.get("arguments", {})
    query = args.get("query", "")

    # Per-agent tool (sled_scoring / sled_docs / sled_deal): the tool name IS the
    # agent selection, so route the whole query straight to that backend.
    if name in _AGENT_TOOL_NAMES:
        return _route_to_agent(_AGENT_TOOL_NAMES[name], query, bearer_token)

    # Router tool (sled_agent): honor an explicit 'agent:' prefix; otherwise the
    # server analyzes the query and picks an agent (keyword heuristic + default).
    if name == "sled_agent":
        if not query.strip():
            available = ", ".join(_configured_agents())
            return {"isError": True, "content": [{"type": "text", "text": (
                f"Empty query. Ask a question, or prefix it with an agent name "
                f"({available}), e.g. 'scoring: ...'."
            )}]}
        agent_name, remaining = parse_agent_prefix(query)
        if agent_name is None:
            agent_name = choose_agent(query)   # auto-select when unspecified
            remaining = query
        return _route_to_agent(agent_name, remaining, bearer_token)

    return {"isError": True, "content": [{"type": "text", "text": f"Unknown tool: {name}"}]}


def _call_backend(url: str, payload: dict, bearer_token: str) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {bearer_token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=29) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            text = body.get("response") or body.get("answer") or body.get("result") or json.dumps(body)
            return {"content": [{"type": "text", "text": text}]}
    except urllib.error.HTTPError as e:
        return {"isError": True, "content": [{"type": "text", "text": f"Backend error {e.code}: {e.read().decode()}"}]}
    except Exception as e:
        return {"isError": True, "content": [{"type": "text", "text": f"Backend error: {str(e)}"}]}

# ── JSON-RPC helpers ──────────────────────────────────────────────────────────

def _ok(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id, code: int, message: str):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _resp(status: int, body: dict, extra_headers: Optional[dict] = None) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Expose-Headers": _EXPOSE_HEADERS,
    }
    if extra_headers:
        headers.update(extra_headers)
    return {"statusCode": status, "headers": headers, "body": json.dumps(body)}




def _initialize_headers(response: dict, incoming_session_id: str = "") -> dict:
    result = response.get("result") if isinstance(response, dict) else None
    if not isinstance(result, dict) or "protocolVersion" not in result:
        return {}

    headers = {"MCP-Protocol-Version": result["protocolVersion"]}

    # By default do NOT send MCP-Session-Id: per the 2025-11-25 spec, if the
    # server sends a session ID the client MUST open a GET SSE stream, which a
    # stateless Lambda can't hold open — so omitting it keeps the client in
    # POST-only mode. MCP_STATEFUL_SESSIONS flips this ON to test whether Otto's
    # client needs a session id to stop its initialize-only reconnect loop. We
    # mint an opaque id (stateless — no server-side store) or reuse the client's.
    if MCP_STATEFUL_SESSIONS:
        headers["MCP-Session-Id"] = incoming_session_id or uuid.uuid4().hex

    return headers


def _accepted_response(extra_headers: Optional[dict] = None) -> dict:
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Expose-Headers": _EXPOSE_HEADERS,
    }
    if extra_headers:
        headers.update(extra_headers)
    return {"statusCode": 202, "headers": headers, "body": ""}


def _dispatch_rpc(message, claims: dict, token: str):
    if not isinstance(message, dict):
        _log("rpc", status="invalid_message", message_type=type(message).__name__)
        return _err(None, -32600, "Invalid Request")

    req_id = message.get("id")
    rpc = message.get("method", "")
    params = message.get("params", {})

    _log("rpc", method=rpc, has_id=req_id is not None)

    if rpc.startswith("notifications/"):
        if rpc == "notifications/initialized":
            _log("rpc", status="initialized_received")
        return None

    if rpc == "initialize":
        client_info = params.get("clientInfo") or {}
        _log(
            "initialize",
            protocol_version=params.get("protocolVersion"),
            client_name=client_info.get("name"),
            client_version=client_info.get("version"),
        )
        return _ok(req_id, handle_initialize(params, claims))
    if rpc == "ping":
        return _ok(req_id, {})
    if rpc == "tools/list":
        return _ok(req_id, handle_tools_list(params, claims))
    if rpc == "tools/call":
        return _ok(req_id, handle_tools_call(params, claims, token))
    if rpc == "resources/list":
        return _ok(req_id, handle_resources_list(params, claims))
    if rpc == "prompts/list":
        return _ok(req_id, handle_prompts_list(params, claims))

    return _err(req_id, -32601, f"Method not found: {rpc}")

# ── Lambda entry point ────────────────────────────────────────────────────────

def lambda_handler(event, _context):
    method = _method(event)
    path   = _path(event)
    base   = _base_url(event)
    headers = _headers(event)
    raw_body = event.get("body") or ""
    session_id = _header_value(headers, "mcp-session-id")
    protocol_version = _header_value(headers, "mcp-protocol-version") or MCP_PROTOCOL_VERSION

    _log(
        "request",
        method=method,
        path=path,
        base=base,
        accept=_header_value(headers, "accept")[:160],
        content_type=_header_value(headers, "content-type")[:120],
        mcp_protocol_version=_header_value(headers, "mcp-protocol-version"),
        mcp_session_id_len=len(session_id),
        has_last_event_id=bool(_header_value(headers, "last-event-id")),
        has_auth=extract_bearer(event) is not None,
        body_len=len(raw_body),
        is_base64=bool(event.get("isBase64Encoded")),
    )

    # CORS preflight
    if method == "OPTIONS":
        return {
            "statusCode": 200,
            "headers": {
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": (
                    "Content-Type, Authorization, MCP-Protocol-Version, "
                    "Mcp-Session-Id, Last-Event-ID"
                ),
                "Access-Control-Expose-Headers": _EXPOSE_HEADERS,
            },
            "body": "",
        }

    # MCP session termination (stateful mode only): a client may DELETE /mcp
    # with an Mcp-Session-Id to end the session. We hold no server-side state,
    # so acknowledge it. Only active when MCP_STATEFUL_SESSIONS is on — otherwise
    # DELETE falls through to the 404 below (unchanged stateless behavior).
    if method == "DELETE" and MCP_STATEFUL_SESSIONS and path.rstrip("/").endswith("/mcp"):
        return _resp(200, {"status": "session_terminated"})

    # Authorization Server Metadata — unauthenticated (RFC 8414 / OIDC alias)
    if method == "GET" and (
        "oauth-authorization-server" in path or
        "openid-configuration" in path
    ):
        return _resp(200, _as_metadata(base))

    # Protected Resource Metadata — unauthenticated (RFC 9728)
    if method == "GET" and "oauth-protected-resource" in path:
        return _resp(200, _prm(base))

    # MCP endpoint discovery probe — requires a bearer token and returns the
    # RFC 9728 challenge when unauthenticated.
    if method == "GET" and path.rstrip("/").endswith("/mcp"):
        token = extract_bearer(event)
        if _accepts(headers, "text/event-stream"):
            # Lambda can't hold open a persistent SSE stream — returning 405
            # tells the client "no server-initiated streaming; use POST only."
            # Without this, the client sees retry: N, reconnects endlessly,
            # and restarts the whole handshake each time.
            _log("transport", method=method, path=path, status="sse_not_supported")
            return sse_not_supported_response()

        if not token:
            _log("auth", method=method, path=path, status="missing")
            return unauthorized_response(base)

        try:
            _log("auth", method=method, path=path, status="present", **_token_summary(token))
            verify_token(token)
            _log("auth", method=method, path=path, status="valid")
        except jwt.ExpiredSignatureError:
            _log("auth", method=method, path=path, status="expired")
            return {
                "statusCode": 401,
                "headers": {
                    "Content-Type": "application/json",
                    "WWW-Authenticate": 'Bearer error="invalid_token", error_description="Token expired"',
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Expose-Headers": "WWW-Authenticate",
                },
                "body": json.dumps({"error": "token_expired"}),
            }
        except Exception as e:
            _log("auth", method=method, path=path, status="invalid", error=type(e).__name__)
            return unauthorized_response(base)

        return _resp(200, health_response())

    # Health check for root/health only.
    if method == "GET" and (path in ("", "/") or path.rstrip("/").endswith("/health")):
        return _resp(200, {"server": MCP_SERVER_NAME, "version": MCP_SERVER_VERSION, "status": "ok"})

    # MCP JSON-RPC — requires valid w3id JWT
    if method == "POST":
        token = extract_bearer(event)
        if not token:
            _log("auth", method=method, path=path, status="missing")
            return unauthorized_response(base)

        try:
            _log("auth", method=method, path=path, status="present", **_token_summary(token))
            claims = verify_token(token)
            _log("auth", method=method, path=path, status="valid")
        except jwt.ExpiredSignatureError:
            _log("auth", method=method, path=path, status="expired")
            return {
                "statusCode": 401,
                "headers": {
                    "Content-Type": "application/json",
                    "WWW-Authenticate": 'Bearer error="invalid_token", error_description="Token expired"',
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Expose-Headers": "WWW-Authenticate",
                },
                "body": json.dumps({"error": "token_expired"}),
            }
        except Exception as e:
            _log("auth", method=method, path=path, status="invalid", error=type(e).__name__)
            return unauthorized_response(base)

        try:
            body = json.loads(_body(event))
        except json.JSONDecodeError:
            _log("rpc", status="invalid_json", body_len=len(raw_body))
            return _resp(400, {"error": "invalid_json"})

        if isinstance(body, list):
            _log(
                "rpc_batch",
                count=len(body),
                methods=[message.get("method") for message in body if isinstance(message, dict)],
            )
            responses = [
                response
                for response in (_dispatch_rpc(message, claims, token) for message in body)
                if response is not None
            ]
            if not responses:
                return _accepted_response({"MCP-Session-Id": session_id} if session_id else None)
            return {
                "statusCode": 200,
                "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
                "body": json.dumps(responses),
            }

        response = _dispatch_rpc(body, claims, token)
        if response is None:
            extra_headers = {
                "MCP-Protocol-Version": protocol_version,
            }
            if session_id:
                extra_headers["MCP-Session-Id"] = session_id
            return _accepted_response(extra_headers)

        extra_headers = _initialize_headers(response, session_id)
        # In stateful mode, echo the session id on every (non-initialize)
        # response so the client keeps reusing the same session.
        if MCP_STATEFUL_SESSIONS and session_id and "MCP-Session-Id" not in extra_headers:
            extra_headers["MCP-Session-Id"] = session_id
        # Always return JSON — never SSE — for POST responses on a stateless
        # Lambda. If we return text/event-stream, Otto opens an SSE stream that
        # closes the moment Lambda terminates, which Otto interprets as a
        # session drop and immediately restarts the whole handshake.
        return _resp(200, response, extra_headers)

    return _resp(404, {"error": "not_found"})
