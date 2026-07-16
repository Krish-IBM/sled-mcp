#!/usr/bin/env bash
# Deploy the SLED competitor-analysis backend and wire it into the MCP router.
#
# Creates (idempotently): the sled-competitor-analysis-agent Lambda and an HTTP
# API in front of it, REUSING the existing sled-scoring-agent-role (same pattern
# as sled-bid-analysis-agent — no IAM writes, so no admin needed). Merges
# COMPETITOR_ANALYSIS_URL into sled-mcp-server's env (never overwrites it).
#
# The role's identity policy already grants: logs, S3 RW on
# sled-scoring-agent-bucket, S3 read on competitive-intelligence-sled, Bedrock
# InvokeModel, Textract. Self-invoke for the async job model is granted via a
# resource-based policy on THIS function (the role's own lambda:InvokeFunction
# statement only names sled-scoring-agent).
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
LAMBDA_NAME="${LAMBDA_NAME:-sled-competitor-analysis-agent}"
ROLE_NAME="${ROLE_NAME:-sled-scoring-agent-role}"
MCP_LAMBDA_NAME="${MCP_LAMBDA_NAME:-sled-mcp-server}"
CA_OUTPUT_BUCKET="${CA_OUTPUT_BUCKET:-sled-scoring-agent-bucket}"
CI_BUCKET="${CI_BUCKET:-competitive-intelligence-sled}"
CA_MODEL_ID="${CA_MODEL_ID:-us.anthropic.claude-sonnet-4-20250514-v1:0}"
CA_FAST_MODEL_ID="${CA_FAST_MODEL_ID:-$CA_MODEL_ID}"
MEMORY="${MEMORY:-3008}"
TIMEOUT="${TIMEOUT:-900}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ZIP="$HERE/build/competitor_analysis.zip"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
LAMBDA_ARN="arn:aws:lambda:${AWS_REGION}:${ACCOUNT_ID}:function:${LAMBDA_NAME}"
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"

echo "Account $ACCOUNT_ID | region $AWS_REGION | role $ROLE_NAME"

# ── 1. build ──────────────────────────────────────────────────────────────── #
[[ -f "$ZIP" && "${SKIP_BUILD:-}" == "1" ]] || "$HERE/build_competitor_package.sh"

# ── 2. Lambda create/update ───────────────────────────────────────────────── #
ENV_VARS="CA_OUTPUT_BUCKET=${CA_OUTPUT_BUCKET},CI_BUCKET=${CI_BUCKET}"
ENV_VARS+=",CA_MODEL_ID=${CA_MODEL_ID},CA_FAST_MODEL_ID=${CA_FAST_MODEL_ID}"
ENV_VARS+=",BEDROCK_REGION=${AWS_REGION},DEFAULT_FOCAL=IBM,SELF_FUNCTION_NAME=${LAMBDA_NAME}"

if aws lambda get-function --function-name "$LAMBDA_NAME" >/dev/null 2>&1; then
  echo "[lambda] updating code + config"
  aws lambda update-function-code --function-name "$LAMBDA_NAME" \
    --zip-file "fileb://$ZIP" --publish >/dev/null
  aws lambda wait function-updated --function-name "$LAMBDA_NAME"
  aws lambda update-function-configuration --function-name "$LAMBDA_NAME" \
    --handler competitor_analysis.handler.lambda_handler --runtime python3.12 \
    --role "$ROLE_ARN" --timeout "$TIMEOUT" --memory-size "$MEMORY" \
    --environment "Variables={$ENV_VARS}" >/dev/null
else
  echo "[lambda] creating $LAMBDA_NAME"
  aws lambda create-function --function-name "$LAMBDA_NAME" \
    --runtime python3.12 --handler competitor_analysis.handler.lambda_handler \
    --role "$ROLE_ARN" --timeout "$TIMEOUT" --memory-size "$MEMORY" \
    --zip-file "fileb://$ZIP" --environment "Variables={$ENV_VARS}" >/dev/null
fi
aws lambda wait function-updated --function-name "$LAMBDA_NAME"

# self-invoke permission (role identity policy only names sled-scoring-agent)
aws lambda add-permission --function-name "$LAMBDA_NAME" --statement-id self-invoke \
  --action lambda:InvokeFunction --principal "$ROLE_ARN" >/dev/null 2>&1 \
  || aws lambda add-permission --function-name "$LAMBDA_NAME" --statement-id self-invoke-acct \
       --action lambda:InvokeFunction --principal "$ACCOUNT_ID" >/dev/null 2>&1 || true

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

COMPETITOR_ANALYSIS_URL="https://${API_ID}.execute-api.${AWS_REGION}.amazonaws.com/"
echo
echo "=================================================================="
echo " COMPETITOR_ANALYSIS_URL = $COMPETITOR_ANALYSIS_URL"
echo "=================================================================="

# ── 4. wire into the MCP router (MERGE env — a bare update wipes it) ────────── #
if aws lambda get-function --function-name "$MCP_LAMBDA_NAME" >/dev/null 2>&1; then
  echo "[wire] merging COMPETITOR_ANALYSIS_URL into $MCP_LAMBDA_NAME env"
  CUR=$(aws lambda get-function-configuration --function-name "$MCP_LAMBDA_NAME" \
        --query 'Environment.Variables' --output json)
  MERGED=$(python3 -c "import json,sys; d=json.load(sys.stdin); d['COMPETITOR_ANALYSIS_URL']='$COMPETITOR_ANALYSIS_URL'; print(json.dumps({'Variables':d}))" <<<"$CUR")
  aws lambda update-function-configuration --function-name "$MCP_LAMBDA_NAME" \
    --environment "$MERGED" >/dev/null
  echo "[wire] done. The router code must include the competitor_analysis registry entry."
else
  echo "[wire] $MCP_LAMBDA_NAME not found — set COMPETITOR_ANALYSIS_URL=$COMPETITOR_ANALYSIS_URL manually."
fi
