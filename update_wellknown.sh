#!/usr/bin/env bash
# =============================================================================
# update_wellknown.sh — Add .well-known endpoint to existing MCP deployment
# =============================================================================

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
AWS_REGION="us-east-1"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
API_GW_ID="zie08z9fuj"
STAGE="sled-docs-query"

LAMBDA_NAME="sled-mcp-server"
LAMBDA_ROLE_NAME="sled-mcp-server-role"

# Your existing Cognito credentials
POOL_ID="us-east-1_50uwMq7nP"
TOKEN_URL="https://sled-mcp-468742.auth.us-east-1.amazoncognito.com/oauth2/token"
SLED_DOCS_QUERY_URL="https://${API_GW_ID}.execute-api.${AWS_REGION}.amazonaws.com/sled-docs-query/sled-docs-query"

echo "============================================="
echo " SLED MCP Server — Update .well-known"
echo " Account : $ACCOUNT_ID"
echo " Region  : $AWS_REGION"
echo "============================================="

# ── Step 1: Update Lambda with new code and env var ──────────────────────────
echo ""
echo "[1/4] Updating Lambda with .well-known support"

cd "$(dirname "$0")"
zip -q mcp_server.zip lambda_handler.py

aws lambda update-function-code \
  --function-name "$LAMBDA_NAME" \
  --zip-file fileb://mcp_server.zip \
  --region "$AWS_REGION" > /dev/null

echo "      Waiting for code update to complete..."
aws lambda wait function-updated --function-name "$LAMBDA_NAME" --region "$AWS_REGION"

aws lambda update-function-configuration \
  --function-name "$LAMBDA_NAME" \
  --environment "Variables={SLED_DOCS_QUERY_URL=$SLED_DOCS_QUERY_URL,COGNITO_TOKEN_URL=$TOKEN_URL}" \
  --region "$AWS_REGION" > /dev/null

echo "      Waiting for configuration update to complete..."
aws lambda wait function-updated --function-name "$LAMBDA_NAME" --region "$AWS_REGION"

LAMBDA_ARN="arn:aws:lambda:${AWS_REGION}:${ACCOUNT_ID}:function:${LAMBDA_NAME}"
echo "      Lambda updated: $LAMBDA_ARN"

rm -f mcp_server.zip

# ── Step 2: Add .well-known routes to API Gateway ─────────────────────────────
echo ""
echo "[2/4] Adding .well-known routes to API Gateway"

# Get root resource
ROOT_ID=$(aws apigateway get-resources \
  --rest-api-id "$API_GW_ID" \
  --region "$AWS_REGION" \
  --query "items[?path=='/'].id" \
  --output text)

# Create /.well-known resource
WELL_KNOWN_RESOURCE_ID=$(aws apigateway get-resources \
  --rest-api-id "$API_GW_ID" \
  --region "$AWS_REGION" \
  --query "items[?path=='/.well-known'].id" \
  --output text)

if [ -z "$WELL_KNOWN_RESOURCE_ID" ]; then
  WELL_KNOWN_RESOURCE_ID=$(aws apigateway create-resource \
    --rest-api-id "$API_GW_ID" \
    --parent-id "$ROOT_ID" \
    --path-part ".well-known" \
    --region "$AWS_REGION" \
    --query "id" \
    --output text)
  echo "      Created /.well-known: $WELL_KNOWN_RESOURCE_ID"
else
  echo "      /.well-known exists: $WELL_KNOWN_RESOURCE_ID"
fi

# Create /.well-known/oauth-authorization-server resource
OAUTH_RESOURCE_ID=$(aws apigateway get-resources \
  --rest-api-id "$API_GW_ID" \
  --region "$AWS_REGION" \
  --query "items[?path=='/.well-known/oauth-authorization-server'].id" \
  --output text)

if [ -z "$OAUTH_RESOURCE_ID" ]; then
  OAUTH_RESOURCE_ID=$(aws apigateway create-resource \
    --rest-api-id "$API_GW_ID" \
    --parent-id "$WELL_KNOWN_RESOURCE_ID" \
    --path-part "oauth-authorization-server" \
    --region "$AWS_REGION" \
    --query "id" \
    --output text)
  echo "      Created oauth-authorization-server: $OAUTH_RESOURCE_ID"
else
  echo "      oauth-authorization-server exists: $OAUTH_RESOURCE_ID"
fi

# ── Step 3: Configure GET method (no auth) ────────────────────────────────────
echo ""
echo "[3/4] Configuring GET method for .well-known endpoint"

aws apigateway put-method \
  --rest-api-id "$API_GW_ID" \
  --resource-id "$OAUTH_RESOURCE_ID" \
  --http-method GET \
  --authorization-type NONE \
  --region "$AWS_REGION" > /dev/null 2>/dev/null || true

aws apigateway put-integration \
  --rest-api-id "$API_GW_ID" \
  --resource-id "$OAUTH_RESOURCE_ID" \
  --http-method GET \
  --type AWS_PROXY \
  --integration-http-method POST \
  --uri "arn:aws:apigateway:${AWS_REGION}:lambda:path/2015-03-31/functions/${LAMBDA_ARN}/invocations" \
  --region "$AWS_REGION" > /dev/null 2>/dev/null || true

# Add Lambda permission
aws lambda add-permission \
  --function-name "$LAMBDA_NAME" \
  --statement-id "apigateway-wellknown-invoke" \
  --action "lambda:InvokeFunction" \
  --principal "apigateway.amazonaws.com" \
  --source-arn "arn:aws:execute-api:${AWS_REGION}:${ACCOUNT_ID}:${API_GW_ID}/*/*/*" \
  --region "$AWS_REGION" > /dev/null 2>/dev/null || true

echo "      Method configured"

# ── Step 4: Deploy to stage ───────────────────────────────────────────────────
echo ""
echo "[4/4] Deploying to stage: $STAGE"

aws apigateway create-deployment \
  --rest-api-id "$API_GW_ID" \
  --stage-name "$STAGE" \
  --region "$AWS_REGION" > /dev/null

WELL_KNOWN_URL="https://${API_GW_ID}.execute-api.${AWS_REGION}.amazonaws.com/${STAGE}/.well-known/oauth-authorization-server"

echo ""
echo "============================================="
echo " ✅ Update Complete"
echo "============================================="
echo ""
echo " .well-known URL: $WELL_KNOWN_URL"
echo ""
echo " Test the endpoint:"
echo "   curl $WELL_KNOWN_URL"
echo ""

# Made with Bob
