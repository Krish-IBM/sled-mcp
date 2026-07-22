# sled-general-agent

The general-purpose ("catch-all") backend behind the MCP router's
`GENERAL_AGENT_URL` (registry key `general`, tool `sled_general`). It answers
open-ended questions the specialized agents don't cover, so a query that fits no
specialist no longer falls back to the corpus-locked docs RAG (which refuses on
anything outside its search results).

## How it answers (corpus-only)

It answers **strictly from the SLED corpus (the knowledge base / S3 bucket)** and
never from the model's own general knowledge. If the corpus has nothing on-topic,
it says so rather than guessing.

1. Parse `query` from the request body (handles the HTTP-API proxy shape where the
   payload is a JSON string in `event["body"]`).
2. **Corpus, best-effort** (`retrieve.py`):
   - Try a direct `bedrock-agent-runtime.retrieve` against KB `FIFNL0U11I`. If the
     top result clears `GENERAL_RETRIEVE_MIN_SCORE` (default 0.4), its chunks
     become the corpus context (`engine: corpus`, `corpus_source: retrieve`).
   - If retrieve raises (e.g. **AccessDenied** — this Lambda's role
     `sled-scoring-agent-role` has Bedrock InvokeModel but may lack
     `bedrock:Retrieve`), fall back to POSTing the existing `sled-docs-query`
     endpoint (`SLED_DOCS_QUERY_URL`), forwarding the caller's bearer
     (`corpus_source: docs`).
   - If retrieve works but nothing is on-topic, no corpus context is used
     (`engine: none`).
3. **Generate**: one `BedrockClient.converse` call (Sonnet 4) with a system prompt
   that answers **only** from the corpus material and cites it. When no corpus
   material is available, it tells the user the SLED corpus does not cover the
   topic (no general-knowledge answer), and points users to a specialized tool
   when the request is really a scoring/competitor/debrief job.

Response: `{"response": "...", "engine": "corpus|none", "corpus_source": "retrieve|docs|"}`.

## Config (env vars)

| Var | Default | Purpose |
|---|---|---|
| `GENERAL_MODEL_ID` | `us.anthropic.claude-sonnet-4-20250514-v1:0` | Bedrock model (Converse) |
| `GENERAL_KB_ID` | `FIFNL0U11I` | SLED CI knowledge base |
| `GENERAL_RETRIEVE_MIN_SCORE` | `0.4` | relevance floor for the retrieve path |
| `SLED_DOCS_QUERY_URL` | — | docs endpoint used as the corpus fallback |
| `GENERAL_MAX_TOKENS` | `2000` | answer length cap |
| `BEDROCK_REGION` | `us-east-1` | region for boto3 clients |

## Build & deploy

```bash
./build_general_package.sh          # -> build/general_agent.zip (bundles scoring_agent for BedrockClient)
./deploy_general_agent.sh           # create/update sled-general-agent + HTTP API, merge router env
# then redeploy the router CODE so the `general` registry entry ships:
zip build/mcp_server.zip lambda_handler.py env_config.py
aws lambda update-function-code --function-name sled-mcp-server --region us-east-1 \
  --zip-file fileb://build/mcp_server.zip --publish
```

`deploy_general_agent.sh` reuses `sled-scoring-agent-role` (no IAM writes) and
**merges** `GENERAL_AGENT_URL` + `DEFAULT_AGENT=general` into the router env
(never a bare overwrite — that wipes the env).

## Tests

`python -m pytest tests/test_general_offline.py` — mocks retrieve + converse and
covers: body-proxy parsing, corpus branch (score ≥ threshold), general branch
(low/empty), docs-endpoint fallback on AccessDenied, and the response shape.

## Rollback

The Lambda is new — deleting it and removing `GENERAL_AGENT_URL` from the router
(and reverting `DEFAULT_AGENT` to `docs`) fully reverts. Reverting `DEFAULT_AGENT`
alone restores the old catch-all without a code change.

## Future optimization

If an admin grants `sled-scoring-agent-role` `bedrock:Retrieve` on KB
`FIFNL0U11I`, the direct-retrieve path activates automatically (one retrieve +
one generate, with real relevance scores) and the docs-endpoint fallback stops
being exercised.
