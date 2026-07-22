#!/usr/bin/env bash
# Deploy the SLED general-purpose ("catch-all") agent and wire it into the router.
#
# Creates (idempotently): the sled-general-agent Lambda and an HTTP API in front
# of it, REUSING the existing sled-scoring-agent-role (same pattern as
# sled-competitor-analysis-agent — no IAM writes, so no admin needed). Merges
# GENERAL_AGENT_URL and DEFAULT_AGENT=general into sled-mcp-server's env
# (never overwrites — a bare update wipes the whole env).
#
# The role already grants Bedrock InvokeModel (Converse), which is all this agent
# strictly needs. It also TRIES a direct KB retrieve; if the role lacks
# bedrock:Retrieve it degrades to POSTing the docs endpoint (SLED_DOCS_QUERY_URL),
# so nothing here depends on an IAM change.
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
LAMBDA_NAME="${LAMBDA_NAME:-sled-general-agent}"
ROLE_NAME="${ROLE_NAME:-sled-scoring-agent-role}"
MCP_LAMBDA_NAME="${MCP_LAMBDA_NAME:-sled-mcp-server}"
GENERAL_MODEL_ID="${GENERAL_MODEL_ID:-us.anthropic.claude-sonnet-4-20250514-v1:0}"
GENERAL_KB_ID="${GENERAL_KB_ID:-FIFNL0U11I}"
GENERAL_RETRIEVE_MIN_SCORE="${GENERAL_RETRIEVE_MIN_SCORE:-0.4}"
DOCS_QUERY_URL="${DOCS_QUERY_URL:-https://zie08z9fuj.execute-api.us-east-1.amazonaws.com/sled-docs-query/sled-docs-query}"
MEMORY="${MEMORY:-512}"
TIMEOUT="${TIMEOUT:-29}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ZIP="$HERE/build/general_agent.zip"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
LAMBDA_ARN="arn:aws:lambda:${AWS_REGION}:${ACCOUNT_ID}:function:${LAMBDA_NAME}"
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"

echo "Account $ACCOUNT_ID | region $AWS_REGION | role $ROLE_NAME"

# ── 1. build ──────────────────────────────────────────────────────────────── #
[[ -f "$ZIP" && "${SKIP_BUILD:-}" == "1" ]] || "$HERE/build_general_package.sh"

# ── 2. Lambda create/update ───────────────────────────────────────────────── #
ENV_VARS="GENERAL_MODEL_ID=${GENERAL_MODEL_ID},GENERAL_KB_ID=${GENERAL_KB_ID}"
ENV_VARS+=",GENERAL_RETRIEVE_MIN_SCORE=${GENERAL_RETRIEVE_MIN_SCORE}"
ENV_VARS+=",SLED_DOCS_QUERY_URL=${DOCS_QUERY_URL},BEDROCK_REGION=${AWS_REGION}"

if aws lambda get-function --function-name "$LAMBDA_NAME" >/dev/null 2>&1; then
  echo "[lambda] updating code + config"
  aws lambda update-function-code --function-name "$LAMBDA_NAME" \
    --zip-file "fileb://$ZIP" --publish >/dev/null
  aws lambda wait function-updated --function-name "$LAMBDA_NAME"
  aws lambda update-function-configuration --function-name "$LAMBDA_NAME" \
    --handler general_agent.handler.lambda_handler --runtime python3.12 \
    --role "$ROLE_ARN" --timeout "$TIMEOUT" --memory-size "$MEMORY" \
    --environment "Variables={$ENV_VARS}" >/dev/null
else
  echo "[lambda] creating $LAMBDA_NAME"
  aws lambda create-function --function-name "$LAMBDA_NAME" \
    --runtime python3.12 --handler general_agent.handler.lambda_handler \
    --role "$ROLE_ARN" --timeout "$TIMEOUT" --memory-size "$MEMORY" \
    --zip-file "fileb://$ZIP" --environment "Variables={$ENV_VARS}" >/dev/null
fi
aws lambda wait function-updated --function-name "$LAMBDA_NAME"

# ── 3. HTTP API in front of the Lambda ────────────────────────────────────── #
API_ID="$(aws apigatewayv2 get-apis --query "Items[?Name=='${LAMBDA_NAME}-api'].ApiId | [0]" --output text)"
if [[ "$API_ID" == "None" || -z "$API_ID" ]]; then
  echo "[apigw] quick-creating HTTP API"
  API_ID="$(aws apigatewayv2 create-api --name "${LAMBDA_NAME}-api" --protocol-type HTTP \
    --target "$LAMBDA_ARN" --query ApiId --output text)"
fi
aws lambda add-permission --function-name "$LAMBDA_NAME" \
  --statement-id apigw-invoke --action lambda:InvokeFunction \
  --principal apigateway.amazonaws.com \
  --source-arn "arn:aws:execute-api:${AWS_REGION}:${ACCOUNT_ID}:${API_ID}/*" >/dev/null 2>&1 || true

GENERAL_AGENT_URL="https://${API_ID}.execute-api.${AWS_REGION}.amazonaws.com/"
echo
echo "=================================================================="
echo " GENERAL_AGENT_URL = $GENERAL_AGENT_URL"
echo "=================================================================="

# ── 4. wire into the MCP router (MERGE env — a bare update wipes it) ────────── #
if aws lambda get-function --function-name "$MCP_LAMBDA_NAME" >/dev/null 2>&1; then
  echo "[wire] merging GENERAL_AGENT_URL + DEFAULT_AGENT=general into $MCP_LAMBDA_NAME env"
  CUR=$(aws lambda get-function-configuration --function-name "$MCP_LAMBDA_NAME" \
        --query 'Environment.Variables' --output json)
  MERGED=$(GENERAL_AGENT_URL="$GENERAL_AGENT_URL" python3 -c "import json,os,sys; d=json.load(sys.stdin); d['GENERAL_AGENT_URL']=os.environ['GENERAL_AGENT_URL']; d['DEFAULT_AGENT']='general'; print(json.dumps({'Variables':d}))" <<<"$CUR")
  aws lambda update-function-configuration --function-name "$MCP_LAMBDA_NAME" \
    --environment "$MERGED" >/dev/null
  echo "[wire] done. Redeploy the router CODE so the general registry entry ships."
else
  echo "[wire] $MCP_LAMBDA_NAME not found — set GENERAL_AGENT_URL=$GENERAL_AGENT_URL + DEFAULT_AGENT=general manually."
fi
