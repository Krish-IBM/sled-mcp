# sled-docs-query (SLED CI "docs" agent backend)

Lambda behind the MCP router's `SLED_DOCS_QUERY_URL` — the `@SLED_CI` / `sled_docs`
agent. Receives `{"query": "..."}` (POST) and returns `{"response": "..."}`.

## What changed (2026-07-10)

Previously this Lambda called a **Bedrock Agent** (`WFJKWV1RKB` alias `SNMYYM5VOU`)
running **Claude 3 Sonnet**, which hard-refused ("Sorry, I am unable to assist you
with this request." / "I could not find information…") even when the knowledge base
returned relevant passages. Verified via agent trace: KB retrieval returned real
docs, then the generation step declined.

`lambda_function.py` now calls **`bedrock-agent-runtime.retrieve_and_generate`**
directly against KB `FIFNL0U11I` (data source = `s3://competitive-intelligence-sled`)
with **Sonnet 4** (`us.anthropic.claude-sonnet-4-20250514-v1:0`) and a custom
anti-refusal prompt that tells the model to synthesize from retrieved passages,
cite the source doc/vendor, and summarize what IS available instead of refusing.

If `retrieve_and_generate` raises (e.g. a missing IAM permission on this Lambda's
role), it **falls back to the original `invoke_agent`** path, so the endpoint is
never worse than before. The response body includes an `engine` field
(`retrieve_and_generate` | `agent_fallback`) for diagnosis.

Why not just fix the Bedrock agent's model/instruction? `update-agent` requires
`iam:PassRole` on `AmazonBedrockAgentRole-sled`, which the `Krish.Chavan@ibm.com`
user is denied (needs admin). Fixing at the Lambda layer avoids that block.

## Deploy

```bash
cd sled_docs_query && zip -X /tmp/docs_query.zip lambda_function.py
aws lambda update-function-code --function-name sled-docs-query --region us-east-1 \
  --zip-file fileb:///tmp/docs_query.zip
aws lambda wait function-updated --function-name sled-docs-query --region us-east-1
```

## Rollback

`lambda_function_ORIGINAL.zip` is the pre-change package (invoke_agent only):

```bash
aws lambda update-function-code --function-name sled-docs-query --region us-east-1 \
  --zip-file fileb://sled_docs_query/lambda_function_ORIGINAL.zip
```
