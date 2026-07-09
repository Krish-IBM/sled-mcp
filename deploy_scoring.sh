#!/usr/bin/env bash
# Deploy the SLED scoring-agent backend and wire it into the MCP router.
#
# Creates (idempotently): an S3 data bucket, an IAM role (S3 + Bedrock + Textract
# + self-invoke + logs), the sled-scoring-agent Lambda, and an HTTP API in front
# of it. Prints the resulting SCORING_AGENT_URL and sets it on sled-mcp-server.
#
# PREREQUISITES you must confirm before running:
#   * AWS CLI configured for the target account/region.
#   * Bedrock model access enabled for the model IDs below.
#   * A project uploaded to  s3://$SCORING_BUCKET/projects/<id>/{rfp,proposals/<Vendor>,scoresheet}/
#
# Usage:
#   SCORING_BUCKET=my-sled-scoring \
#   SCORING_MODEL_ID=<strong-claude-inference-profile> \
#   SCORING_FAST_MODEL_ID=<fast-claude-inference-profile> \
#   ./deploy_scoring.sh
set -euo pipefail

# ── config ────────────────────────────────────────────────────────────────── #
AWS_REGION="${AWS_REGION:-us-east-1}"
LAMBDA_NAME="${LAMBDA_NAME:-sled-scoring-agent}"
ROLE_NAME="${ROLE_NAME:-sled-scoring-agent-role}"
MCP_LAMBDA_NAME="${MCP_LAMBDA_NAME:-sled-mcp-server}"          # to receive SCORING_AGENT_URL
SCORING_BUCKET="${SCORING_BUCKET:?set SCORING_BUCKET to the data bucket name}"
# Claude Sonnet 4 cross-region inference profile — confirmed working in us-east-1.
# Override with SCORING_MODEL_ID / SCORING_FAST_MODEL_ID env vars if you need a different model.
SCORING_MODEL_ID="${SCORING_MODEL_ID:-us.anthropic.claude-sonnet-4-20250514-v1:0}"
SCORING_FAST_MODEL_ID="${SCORING_FAST_MODEL_ID:-us.anthropic.claude-sonnet-4-20250514-v1:0}"
PPTX_TEMPLATE_LOCAL="${PPTX_TEMPLATE_LOCAL:-}"                 # optional local .pptx to bundle+upload
CI_BUCKET="${CI_BUCKET:-competitive-intelligence-sled}"        # source bucket for deal= scoring
CI_DEALS_PREFIX="${CI_DEALS_PREFIX:-}"                         # prefix within CI bucket (empty = folders at root)
MEMORY="${MEMORY:-2048}"
TIMEOUT="${TIMEOUT:-900}"
# If set, reuse this existing execution role instead of creating one. S3 access
# is then granted via a bucket policy and self-invoke via a Lambda resource
# policy, so no IAM-role edits are required.
PROVIDED_ROLE_ARN="${ROLE_ARN:-}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ZIP="$HERE/build/scoring_agent.zip"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
LAMBDA_ARN="arn:aws:lambda:${AWS_REGION}:${ACCOUNT_ID}:function:${LAMBDA_NAME}"

echo "Account $ACCOUNT_ID | region $AWS_REGION | bucket $SCORING_BUCKET"

# ── 1. build the package ──────────────────────────────────────────────────── #
if [[ -n "$PPTX_TEMPLATE_LOCAL" ]]; then
  "$HERE/build_scoring_package.sh" --template "$PPTX_TEMPLATE_LOCAL"
else
  "$HERE/build_scoring_package.sh"
fi

# ── 2. S3 bucket ──────────────────────────────────────────────────────────── #
if ! aws s3api head-bucket --bucket "$SCORING_BUCKET" 2>/dev/null; then
  echo "[s3] creating $SCORING_BUCKET"
  if [[ "$AWS_REGION" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "$SCORING_BUCKET" --region "$AWS_REGION"
  else
    aws s3api create-bucket --bucket "$SCORING_BUCKET" --region "$AWS_REGION" \
      --create-bucket-configuration LocationConstraint="$AWS_REGION"
  fi
fi

TEMPLATE_ENV=""
if [[ -n "$PPTX_TEMPLATE_LOCAL" && -f "$PPTX_TEMPLATE_LOCAL" ]]; then
  aws s3 cp "$PPTX_TEMPLATE_LOCAL" "s3://$SCORING_BUCKET/templates/scorecard.pptx"
  TEMPLATE_ENV=",SCORING_PPTX_TEMPLATE_S3=templates/scorecard.pptx"
fi

# ── 3. Execution role ─────────────────────────────────────────────────────── #
if [[ -n "$PROVIDED_ROLE_ARN" ]]; then
  # Reuse an existing role (it must be Lambda-assumable and allow Bedrock).
  ROLE_ARN="$PROVIDED_ROLE_ARN"
  echo "[iam] reusing provided role: $ROLE_ARN"
  # Grant S3 access to that role via a BUCKET policy (no IAM edits needed).
  BUCKET_POLICY=$(cat <<JSON
{"Version":"2012-10-17","Statement":[{"Sid":"SledScoringAgentAccess","Effect":"Allow",
  "Principal":{"AWS":"${ROLE_ARN}"},
  "Action":["s3:GetObject","s3:PutObject","s3:ListBucket"],
  "Resource":["arn:aws:s3:::${SCORING_BUCKET}","arn:aws:s3:::${SCORING_BUCKET}/*"]}]}
JSON
)
  if aws s3api put-bucket-policy --bucket "$SCORING_BUCKET" --policy "$BUCKET_POLICY" 2>/dev/null; then
    echo "[s3] bucket policy grants the role S3 access"
  else
    echo "[s3] could not set bucket policy (ok if the role already has S3 access)"
  fi
  # Textract perms may be absent on a reused role; scanned PDFs fall back to Bedrock OCR.
else
  TRUST='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
  if ! aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
    echo "[iam] creating role $ROLE_NAME"
    aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document "$TRUST" >/dev/null
  fi
  ROLE_ARN="$(aws iam get-role --role-name "$ROLE_NAME" --query Role.Arn --output text)"
  POLICY=$(cat <<JSON
{"Version":"2012-10-17","Statement":[
  {"Effect":"Allow","Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],"Resource":"*"},
  {"Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:ListBucket"],
   "Resource":["arn:aws:s3:::${SCORING_BUCKET}","arn:aws:s3:::${SCORING_BUCKET}/*"]},
  {"Effect":"Allow","Action":["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream"],"Resource":"*"},
  {"Effect":"Allow","Action":["textract:StartDocumentTextDetection","textract:GetDocumentTextDetection"],"Resource":"*"},
  {"Effect":"Allow","Action":["lambda:InvokeFunction"],"Resource":"${LAMBDA_ARN}"}
]}
JSON
)
  aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name "${ROLE_NAME}-policy" \
    --policy-document "$POLICY"
  echo "[iam] role ready: $ROLE_ARN"
  sleep 8   # allow role propagation
fi

# ── 4. Lambda create/update ───────────────────────────────────────────────── #
ENV_VARS="SCORING_BUCKET=${SCORING_BUCKET},SCORING_OUTPUT_BUCKET=${SCORING_BUCKET}"
ENV_VARS+=",SCORING_MODEL_ID=${SCORING_MODEL_ID},SCORING_FAST_MODEL_ID=${SCORING_FAST_MODEL_ID}"
ENV_VARS+=",BEDROCK_REGION=${AWS_REGION},DEFAULT_FOCAL=IBM,SELF_FUNCTION_NAME=${LAMBDA_NAME}"
ENV_VARS+=",CI_BUCKET=${CI_BUCKET}"
[[ -n "$CI_DEALS_PREFIX" ]] && ENV_VARS+=",CI_DEALS_PREFIX=${CI_DEALS_PREFIX}"
ENV_VARS+="${TEMPLATE_ENV}"

if aws lambda get-function --function-name "$LAMBDA_NAME" >/dev/null 2>&1; then
  echo "[lambda] updating code + config"
  aws lambda update-function-code --function-name "$LAMBDA_NAME" \
    --zip-file "fileb://$ZIP" --publish >/dev/null
  aws lambda wait function-updated --function-name "$LAMBDA_NAME"
  aws lambda update-function-configuration --function-name "$LAMBDA_NAME" \
    --handler scoring_agent.handler.lambda_handler --runtime python3.12 \
    --role "$ROLE_ARN" --timeout "$TIMEOUT" --memory-size "$MEMORY" \
    --environment "Variables={$ENV_VARS}" >/dev/null
else
  echo "[lambda] creating $LAMBDA_NAME"
  aws lambda create-function --function-name "$LAMBDA_NAME" \
    --runtime python3.12 --handler scoring_agent.handler.lambda_handler \
    --role "$ROLE_ARN" --timeout "$TIMEOUT" --memory-size "$MEMORY" \
    --zip-file "fileb://$ZIP" --environment "Variables={$ENV_VARS}" >/dev/null
fi
aws lambda wait function-updated --function-name "$LAMBDA_NAME"

# allow the execution role to invoke this function (async self-invoke). Needed
# when the role lacks lambda:InvokeFunction in its identity policy (reused role).
aws lambda add-permission --function-name "$LAMBDA_NAME" --statement-id self-invoke \
  --action lambda:InvokeFunction --principal "$ROLE_ARN" >/dev/null 2>&1 \
  || aws lambda add-permission --function-name "$LAMBDA_NAME" --statement-id self-invoke-acct \
       --action lambda:InvokeFunction --principal "$ACCOUNT_ID" >/dev/null 2>&1 || true

# ── 5. HTTP API in front of the Lambda ────────────────────────────────────── #
API_ID="$(aws apigatewayv2 get-apis --query "Items[?Name=='${LAMBDA_NAME}-api'].ApiId | [0]" --output text)"
if [[ "$API_ID" == "None" || -z "$API_ID" ]]; then
  echo "[apigw] quick-creating HTTP API"
  API_ID="$(aws apigatewayv2 create-api --name "${LAMBDA_NAME}-api" --protocol-type HTTP \
    --target "$LAMBDA_ARN" --query ApiId --output text)"
fi
# ensure invoke permission (idempotent)
aws lambda add-permission --function-name "$LAMBDA_NAME" \
  --statement-id apigw-invoke --action lambda:InvokeFunction \
  --principal apigateway.amazonaws.com \
  --source-arn "arn:aws:execute-api:${AWS_REGION}:${ACCOUNT_ID}:${API_ID}/*" >/dev/null 2>&1 || true

SCORING_AGENT_URL="https://${API_ID}.execute-api.${AWS_REGION}.amazonaws.com/"
echo
echo "=================================================================="
echo " SCORING_AGENT_URL = $SCORING_AGENT_URL"
echo "=================================================================="

# ── 6. wire into the MCP router ───────────────────────────────────────────── #
if aws lambda get-function --function-name "$MCP_LAMBDA_NAME" >/dev/null 2>&1; then
  echo "[wire] setting SCORING_AGENT_URL on $MCP_LAMBDA_NAME (merging existing env)"
  CUR=$(aws lambda get-function-configuration --function-name "$MCP_LAMBDA_NAME" \
        --query 'Environment.Variables' --output json)
  MERGED=$(python3 -c "import json,sys; d=json.load(sys.stdin); d['SCORING_AGENT_URL']='$SCORING_AGENT_URL'; print(json.dumps({'Variables':d}))" <<<"$CUR")
  aws lambda update-function-configuration --function-name "$MCP_LAMBDA_NAME" \
    --environment "$MERGED" >/dev/null
  echo "[wire] done. Test:  scoring: score project=<id> focal=IBM"
else
  echo "[wire] $MCP_LAMBDA_NAME not found — set SCORING_AGENT_URL=$SCORING_AGENT_URL on your MCP Lambda manually."
fi
