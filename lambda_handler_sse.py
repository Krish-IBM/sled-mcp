"""
MCP Server Lambda — SLED Competitive Intelligence (SSE Transport)
Implements MCP protocol over HTTP with SSE for Otto compatibility.
"""

import json
import os
import urllib.request
import urllib.error
import base64

# ── Config ────────────────────────────────────────────────────────────────────
SLED_DOCS_QUERY_URL = os.environ["SLED_DOCS_QUERY_URL"]
COGNITO_TOKEN_URL = os.environ.get("COGNITO_TOKEN_URL", "")

MCP_SERVER_NAME = "sled-competitive-intel"
MCP_SERVER_VERSION = "1.0.0"

# ── Tool definitions ──────────────────────────────────────────────────────────
TOOLS = [
    {
        "name": "sled_docs_query",
        "description": (
            "Query the SLED competitive intelligence knowledge base. "
            "Use this to retrieve information about competitors, deals, "
            "and state/local/education market intelligence."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language question or search query."
                }
            },
            "required": ["query"]
        }
    }
]

# ── MCP Handlers ──────────────────────────────────────────────────────────────

def handle_initialize(params: dict) -> dict:
    return {
        "protocolVersion": "2024-11-05",
        "serverInfo": {
            "name": MCP_SERVER_NAME,
            "version": MCP_SERVER_VERSION
        },
        "capabilities": {
            "tools": {}
        }
    }


def handle_tools_list(params: dict) -> dict:
    return {"tools": TOOLS}


def handle_tools_call(params: dict) -> dict:
    tool_name = params.get("name")
    arguments = params.get("arguments", {})

    if tool_name == "sled_docs_query":
        return call_sled_docs_query(arguments)

    return {
        "isError": True,
        "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}]
    }


def call_sled_docs_query(arguments: dict) -> dict:
    query = arguments.get("query", "")
    if not query:
        return {
            "isError": True,
            "content": [{"type": "text", "text": "Missing required argument: query"}]
        }

    payload = json.dumps({"query": query}).encode("utf-8")
    req = urllib.request.Request(
        SLED_DOCS_QUERY_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            answer = body.get("response") or body.get("answer") or json.dumps(body)
            return {
                "content": [{"type": "text", "text": answer}]
            }
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"Error {e.code}: {error_body}"}]
        }
    except Exception as e:
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"Internal error: {str(e)}"}]
        }


# ── JSON-RPC Dispatch ─────────────────────────────────────────────────────────

HANDLERS = {
    "initialize": handle_initialize,
    "tools/list": handle_tools_list,
    "tools/call": handle_tools_call,
}


def jsonrpc_response(request_id, result):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": result
    }


def jsonrpc_error(request_id, code: int, message: str):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message}
    }


# ── Lambda Handler ────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    http_method = event.get("httpMethod", "")
    path = event.get("path", "")
    
    # CORS preflight
    if http_method == "OPTIONS":
        return {
            "statusCode": 200,
            "headers": {
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization",
                "Access-Control-Max-Age": "86400"
            },
            "body": ""
        }
    
    # OAuth Discovery endpoint
    if http_method == "GET" and "oauth-authorization-server" in path:
        headers = event.get("headers", {})
        host = headers.get("Host") or headers.get("host", "")
        stage = event.get("requestContext", {}).get("stage", "")
        
        if stage and stage != "$default":
            issuer = f"https://{host}/{stage}"
        else:
            issuer = f"https://{host}"
        
        return {
            "statusCode": 200,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*"
            },
            "body": json.dumps({
                "issuer": issuer,
                "token_endpoint": COGNITO_TOKEN_URL,
                "grant_types_supported": ["client_credentials"],
                "token_endpoint_auth_methods_supported": [
                    "client_secret_post",
                    "client_secret_basic"
                ]
            })
        }
    
    # Health check
    if http_method == "GET" and "/mcp" in path:
        return {
            "statusCode": 200,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*"
            },
            "body": json.dumps({
                "server": MCP_SERVER_NAME,
                "version": MCP_SERVER_VERSION,
                "status": "ok",
                "transport": "http"
            })
        }
    
    # MCP JSON-RPC endpoint (POST /mcp)
    if http_method == "POST":
        try:
            body = json.loads(event.get("body") or "{}")
        except json.JSONDecodeError:
            return {
                "statusCode": 400,
                "headers": {
                    "Content-Type": "application/json",
                    "Access-Control-Allow-Origin": "*"
                },
                "body": json.dumps({"error": "Invalid JSON"})
            }

        rpc_id = body.get("id")
        method = body.get("method", "")
        params = body.get("params", {})

        handler = HANDLERS.get(method)
        if not handler:
            resp = jsonrpc_error(rpc_id, -32601, f"Method not found: {method}")
            return {
                "statusCode": 404,
                "headers": {
                    "Content-Type": "application/json",
                    "Access-Control-Allow-Origin": "*"
                },
                "body": json.dumps(resp)
            }

        try:
            result = handler(params)
            resp = jsonrpc_response(rpc_id, result)
            return {
                "statusCode": 200,
                "headers": {
                    "Content-Type": "application/json",
                    "Access-Control-Allow-Origin": "*"
                },
                "body": json.dumps(resp)
            }
        except Exception as e:
            resp = jsonrpc_error(rpc_id, -32603, f"Internal error: {str(e)}")
            return {
                "statusCode": 500,
                "headers": {
                    "Content-Type": "application/json",
                    "Access-Control-Allow-Origin": "*"
                },
                "body": json.dumps(resp)
            }
    
    # Default 404
    return {
        "statusCode": 404,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*"
        },
        "body": json.dumps({"error": "Not found"})
    }

# Made with Bob
