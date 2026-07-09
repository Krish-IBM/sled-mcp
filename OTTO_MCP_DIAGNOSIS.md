# Otto ↔ SLED MCP — "Tool not found" diagnosis (2026-07-07)

**TL;DR:** The SLED MCP server is healthy. The `mcp_SLED-MCP_sled_agent` "tool not found"
error is caused by **Otto's MCP client falling into an `initialize`-only reconnect loop**:
after a successful `tools/call` it re-handshakes every ~30s but never re-issues `tools/list`,
so the tool disappears from Otto's registry. This is an Otto/Collie client-side issue, not a
server outage. This packet gives the evidence and a proposed (optional) server-side mitigation.

---

## What the user saw (in Otto)

```
@SLED-MCP scoring: score deal=Accenture focal=IBM        → OK (returned job_id)
@SLED-MCP scoring: status <job_id>                       → OK ("running, ingest")
@SLED-MCP scoring: status <job_id>                       → [{"name":"CollieError"}]
      Tool "mcp_SLED-MCP_sled_agent" not found. Hence main aggregation pipeline could
      not complete. Failure in stage 0. Try to call the tool again...
```

`CollieError`, "main aggregation pipeline", "stage 0" are **Otto's own orchestrator strings** —
the SLED Lambda never emits them. The Lambda returned HTTP 200 to every request in this window.

---

## Server is healthy (verified live)

- `GET /mcp` → `401` with a correct RFC 9728 challenge:
  `WWW-Authenticate: Bearer realm="mcp", resource_metadata=".../.well-known/oauth-protected-resource"`.
- `GET /.well-known/oauth-protected-resource` and `/.well-known/oauth-authorization-server`
  return valid metadata.
- Every authenticated request in CloudWatch validated as `auth ... status=valid`
  (w3id token → local JWKS fails as expected for the opaque token → introspection succeeds).

## The actual failure — an `initialize`-only loop (CloudWatch, `sled-mcp-server`)

After the last successful `tools/call`, the client sent a bare `initialize` **every ~30 seconds**
and never advanced to `tools/list` or `tools/call`:

```
14:09:02  POST /mcp  auth=valid  rpc: tools/call      ← last good call ("running, ingest")
14:09:31  POST /mcp  auth=valid  rpc: initialize      ← mcp-protocol-version header now EMPTY
14:10:01  POST /mcp  auth=valid  rpc: initialize
14:10:31  POST /mcp  auth=valid  rpc: initialize
14:11:01  POST /mcp  auth=valid  rpc: initialize
14:11:31  POST /mcp  auth=valid  rpc: initialize
14:12:01  POST /mcp  auth=valid  rpc: initialize
14:12:31  POST /mcp  auth=valid  rpc: initialize
```

Two tells in the loop requests vs. the working one:
1. `mcp-protocol-version` header is **empty** in the loop (it was `2025-11-25` on the good call) —
   the client dropped its negotiated protocol state and restarted the handshake.
2. The client **never sends `notifications/initialized` → `tools/list`** after `initialize`, so it
   never re-registers `sled_agent`. Otto's LLM then reports the tool as missing.

## Expected vs. observed client behavior

| Step | MCP spec (stateless server) | Otto/Collie observed |
|---|---|---|
| 1 | `initialize` | ✅ (repeatedly) |
| 2 | `notifications/initialized` | ❌ never seen |
| 3 | `tools/list` (re-register tools) | ❌ never seen |
| 4 | `tools/call` | ❌ never re-issued |

The server responds `200` to `initialize` with the negotiated `protocolVersion` and, by design,
**no `MCP-Session-Id`** (the Lambda is stateless — see `_initialize_headers` in `lambda_handler.py`;
returning a session id would, per the 2025-11-25 spec, obligate the client to open a GET SSE stream
that a stateless Lambda cannot hold open).

## Questions for the Otto / Collie team

1. Why does the client re-enter `initialize` mid-session instead of reusing the negotiated session?
2. After a fresh `initialize`, why does it not send `tools/list` before deciding the tool is missing?
3. Does the client **require** an `MCP-Session-Id` on the `initialize` response to consider the
   session established? If so, it should treat a stateless (no-session-id) server as valid POST-only,
   not loop. (We can optionally return a session id — see below — if that unblocks it.)
4. Does a token refresh / new turn reset the client's tool registry? The re-mention `@SLED-MCP` in
   every turn should not be required if tools persist across turns.

## Optional server-side mitigation (shipped, OFF by default)

We added an env-gated switch on `sled-mcp-server`: **`MCP_STATEFUL_SESSIONS=1`** makes the server
return an `MCP-Session-Id` on `initialize` and accept it on later POSTs (still POST-only; no SSE).
This exists only to test whether Otto's client needs a session id to stop looping. Enable it on a
**test** connector and watch the CloudWatch trace for `tools/list` following `initialize`. Leave it
**unset** in production unless it demonstrably fixes the loop, since a spec-strict client may then
expect a GET SSE stream the Lambda can't serve.

## Operational workaround (today)

Fully **remove and re-add** the connector in Otto (not just toggle) to reset its session state;
keep polling turns to a minimum (the scoring job model needs `score` → occasional `status` →
`result`, so avoid rapid repeated `status` in separate turns).
