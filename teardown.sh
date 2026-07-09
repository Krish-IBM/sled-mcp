#!/usr/bin/env bash
# =============================================================================
# teardown.sh — Remove all MCP Server resources
# Deletes Lambda, Cognito pool, IAM role, and API Gateway /mcp route
# =============================================================================

set -euo pipefail

AWS_REGION="us-east-1"
API_GW_ID="zie08z9fuj"
STAGE="prod"
LAMBDA_NAME="sled-mcp-server"
LAMBDA_ROLE_NAME="sled-mcp-server-role"
COGNITO_POOL_NAME="sled-mcp-pool"

echo "============================================="
echo " SLED MCP Server — Teardown"
echo "============================================="

# Delete Lambda
echo "[1/5] Deleting Lambda: $LAMBDA_NAME"
aws lambda delete-function --function-name "$LAMBDA_NAME" --region "$AWS_REGION" 2>/dev/null || echo "      (not found, skipping)"

# Delete Cognito pool
echo "[2/5] Deleting Cognito User Pool"
POOL_ID=$(aws cognito-idp list-user-pools --max-results 60 --region "$AWS_REGION" \
  --query "UserPools[?Name=='$COGNITO_POOL_NAME'].Id" --output text 2>/dev/null || true)
if [ -n "$POOL_ID" ]; then
  DOMAIN=$(aws cognito-idp describe-user-pool --user-pool-id "$POOL_ID" --region "$AWS_REGION" \
    --query "UserPool.Domain" --output text 2>/dev/null || true)
  if [ -n "$DOMAIN" ] && [ "$DOMAIN" != "None" ]; then
    aws cognito-idp delete-user-pool-domain --domain "$DOMAIN" --user-pool-id "$POOL_ID" --region "$AWS_REGION" || true
  fi
  aws cognito-idp delete-user-pool --user-pool-id "$POOL_ID" --region "$AWS_REGION" || true
  echo "      Deleted pool: $POOL_ID"
else
  echo "      (not found, skipping)"
fi

# Delete IAM role
echo "[3/5] Deleting IAM role: $LAMBDA_ROLE_NAME"
aws iam detach-role-policy \
  --role-name "$LAMBDA_ROLE_NAME" \
  --policy-arn "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole" 2>/dev/null || true
aws iam delete-role --role-name "$LAMBDA_ROLE_NAME" 2>/dev/null || echo "      (not found, skipping)"

# Remove /mcp resource from API Gateway
echo "[4/5] Removing /mcp route from API Gateway"
MCP_RESOURCE_ID=$(aws apigateway get-resources \
  --rest-api-id "$API_GW_ID" \
  --region "$AWS_REGION" \
  --query "items[?path=='/mcp'].id" \
  --output text 2>/dev/null || true)
if [ -n "$MCP_RESOURCE_ID" ]; then
  aws apigateway delete-resource \
    --rest-api-id "$API_GW_ID" \
    --resource-id "$MCP_RESOURCE_ID" \
    --region "$AWS_REGION" || true
  echo "      Removed /mcp resource"
else
  echo "      (not found, skipping)"
fi

# Remove authorizer
echo "[5/5] Removing Cognito authorizer"
AUTHORIZER_ID=$(aws apigateway get-authorizers \
  --rest-api-id "$API_GW_ID" \
  --region "$AWS_REGION" \
  --query "items[?name=='sled-mcp-cognito-authorizer'].id" \
  --output text 2>/dev/null || true)
if [ -n "$AUTHORIZER_ID" ]; then
  aws apigateway delete-authorizer \
    --rest-api-id "$API_GW_ID" \
    --authorizer-id "$AUTHORIZER_ID" \
    --region "$AWS_REGION" || true
  echo "      Removed authorizer"
  # Redeploy after changes
  aws apigateway create-deployment \
    --rest-api-id "$API_GW_ID" \
    --stage-name "$STAGE" \
    --region "$AWS_REGION" > /dev/null
else
  echo "      (not found, skipping)"
fi

echo ""
echo "✅ Teardown complete"
