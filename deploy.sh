#!/usr/bin/env bash
# =============================================================================
# deploy.sh — MCP Server for SLED Competitive Intelligence
# Provisions Cognito, packages Lambda, wires up API Gateway
#
# Prerequisites:
#   - AWS CLI configured (aws configure)
#   - Existing API Gateway ID: zie08z9fuj
#   - Existing sled-docs-query Lambda + its API Gateway invoke URL
#   - IAM permissions: Lambda, API Gateway, Cognito, IAM
#
# Usage:
#   chmod +x deploy.sh
#   ./deploy.sh
# =============================================================================

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
AWS_REGION="us-east-1"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
API_GW_ID="zie08z9fuj"          # your existing API Gateway
STAGE="sled-docs-query"

LAMBDA_NAME="sled-mcp-server"
LAMBDA_ROLE_NAME="sled-mcp-server-role"

# URL of your existing sled-docs-query endpoint
SLED_DOCS_QUERY_URL="https://${API_GW_ID}.execute-api.${AWS_REGION}.amazonaws.com/sled-docs-query/sled-docs-query"

COGNITO_POOL_NAME="sled-mcp-pool"
COGNITO_CLIENT_NAME="otto-mcp-client"
COGNITO_RESOURCE_SERVER_ID="https://sled-mcp-api"
COGNITO_SCOPE="${COGNITO_RESOURCE_SERVER_ID}/invoke"

echo "============================================="
echo " SLED MCP Server — Deploy"
echo " Account : $ACCOUNT_ID"
echo " Region  : $AWS_REGION"
echo "============================================="

# ── Step 1: Cognito User Pool ─────────────────────────────────────────────────
echo ""
echo "[1/7] Creating Cognito User Pool: $COGNITO_POOL_NAME"

POOL_ID=$(aws cognito-idp create-user-pool \
  --pool-name "$COGNITO_POOL_NAME" \
  --region "$AWS_REGION" \
  --query "UserPool.Id" \
  --output text)

echo "      Pool ID: $POOL_ID"

# ── Step 2: Cognito Resource Server (defines the OAuth scope) ─────────────────
echo ""
echo "[2/7] Creating Resource Server (scope: invoke)"

aws cognito-idp create-resource-server \
  --user-pool-id "$POOL_ID" \
  --identifier "$COGNITO_RESOURCE_SERVER_ID" \
  --name "SLED MCP API" \
  --scopes ScopeName=invoke,ScopeDescription="Invoke SLED MCP tools" \
  --region "$AWS_REGION" > /dev/null

# ── Step 3: Cognito App Client (machine-to-machine, client_credentials) ───────
echo ""
echo "[3/7] Creating App Client: $COGNITO_CLIENT_NAME"

CLIENT=$(aws cognito-idp create-user-pool-client \
  --user-pool-id "$POOL_ID" \
  --client-name "$COGNITO_CLIENT_NAME" \
  --generate-secret \
  --allowed-o-auth-flows client_credentials \
  --allowed-o-auth-scopes "$COGNITO_SCOPE" \
  --allowed-o-auth-flows-user-pool-client \
  --region "$AWS_REGION")

CLIENT_ID=$(echo "$CLIENT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['UserPoolClient']['ClientId'])")
CLIENT_SECRET=$(echo "$CLIENT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['UserPoolClient']['ClientSecret'])")

echo "      Client ID     : $CLIENT_ID"
echo "      Client Secret : $CLIENT_SECRET"

# ── Step 4: Cognito Domain (needed for /oauth2/token endpoint) ────────────────
echo ""
echo "[4/7] Creating Cognito domain"

DOMAIN_PREFIX="sled-mcp-$(echo $ACCOUNT_ID | tail -c 7)"
aws cognito-idp create-user-pool-domain \
  --domain "$DOMAIN_PREFIX" \
  --user-pool-id "$POOL_ID" \
  --region "$AWS_REGION" > /dev/null

TOKEN_URL="https://${DOMAIN_PREFIX}.auth.${AWS_REGION}.amazoncognito.com/oauth2/token"
echo "      Token URL: $TOKEN_URL"

# ── Step 5: IAM Role for MCP Lambda ──────────────────────────────────────────
echo ""
echo "[5/7] Creating IAM role: $LAMBDA_ROLE_NAME"

TRUST_POLICY='{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Service": "lambda.amazonaws.com" },
    "Action": "sts:AssumeRole"
  }]
}'

ROLE_ARN=$(aws iam create-role \
  --role-name "$LAMBDA_ROLE_NAME" \
  --assume-role-policy-document "$TRUST_POLICY" \
  --query "Role.Arn" \
  --output text 2>/dev/null || \
  aws iam get-role --role-name "$LAMBDA_ROLE_NAME" --query "Role.Arn" --output text)

aws iam attach-role-policy \
  --role-name "$LAMBDA_ROLE_NAME" \
  --policy-arn "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole" 2>/dev/null || true

echo "      Role ARN: $ROLE_ARN"
echo "      Waiting 10s for IAM propagation..."
sleep 10

# ── Step 6: Package and Deploy Lambda ─────────────────────────────────────────
echo ""
echo "[6/7] Packaging and deploying Lambda: $LAMBDA_NAME"

cd "$(dirname "$0")"
zip -q mcp_server.zip lambda_handler.py

# Create or update
if aws lambda get-function --function-name "$LAMBDA_NAME" --region "$AWS_REGION" > /dev/null 2>&1; then
  echo "      Updating existing Lambda..."
  aws lambda update-function-code \
    --function-name "$LAMBDA_NAME" \
    --zip-file fileb://mcp_server.zip \
    --region "$AWS_REGION" > /dev/null
  aws lambda update-function-configuration \
    --function-name "$LAMBDA_NAME" \
    --environment "Variables={SLED_DOCS_QUERY_URL=$SLED_DOCS_QUERY_URL,COGNITO_TOKEN_URL=$TOKEN_URL}" \
    --region "$AWS_REGION" > /dev/null
else
  echo "      Creating new Lambda..."
  aws lambda create-function \
    --function-name "$LAMBDA_NAME" \
    --runtime python3.12 \
    --role "$ROLE_ARN" \
    --handler lambda_handler.lambda_handler \
    --zip-file fileb://mcp_server.zip \
    --timeout 30 \
    --memory-size 256 \
    --environment "Variables={SLED_DOCS_QUERY_URL=$SLED_DOCS_QUERY_URL,COGNITO_TOKEN_URL=$TOKEN_URL}" \
    --region "$AWS_REGION" > /dev/null
fi

LAMBDA_ARN="arn:aws:lambda:${AWS_REGION}:${ACCOUNT_ID}:function:${LAMBDA_NAME}"
echo "      Lambda ARN: $LAMBDA_ARN"

rm -f mcp_server.zip

# ── Step 7: Wire up API Gateway ───────────────────────────────────────────────
echo ""
echo "[7/7] Wiring /mcp route on API Gateway $API_GW_ID"

# Get the root resource ID
ROOT_ID=$(aws apigateway get-resources \
  --rest-api-id "$API_GW_ID" \
  --region "$AWS_REGION" \
  --query "items[?path=='/'].id" \
  --output text)

# Create /mcp resource (skip if exists)
MCP_RESOURCE_ID=$(aws apigateway get-resources \
  --rest-api-id "$API_GW_ID" \
  --region "$AWS_REGION" \
  --query "items[?path=='/mcp'].id" \
  --output text)

if [ -z "$MCP_RESOURCE_ID" ]; then
  MCP_RESOURCE_ID=$(aws apigateway create-resource \
    --rest-api-id "$API_GW_ID" \
    --parent-id "$ROOT_ID" \
    --path-part "mcp" \
    --region "$AWS_REGION" \
    --query "id" \
    --output text)
  echo "      Created /mcp resource: $MCP_RESOURCE_ID"
else
  echo "      /mcp resource already exists: $MCP_RESOURCE_ID"
fi

# Create /.well-known resource (for OAuth discovery)
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
  echo "      Created /.well-known resource: $WELL_KNOWN_RESOURCE_ID"
else
  echo "      /.well-known resource already exists: $WELL_KNOWN_RESOURCE_ID"
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
  echo "      Created /.well-known/oauth-authorization-server resource: $OAUTH_RESOURCE_ID"
else
  echo "      /.well-known/oauth-authorization-server resource already exists: $OAUTH_RESOURCE_ID"
fi

# Create Cognito JWT Authorizer
AUTHORIZER_ID=$(aws apigateway create-authorizer \
  --rest-api-id "$API_GW_ID" \
  --name "sled-mcp-cognito-authorizer" \
  --type COGNITO_USER_POOLS \
  --provider-arns "arn:aws:cognito-idp:${AWS_REGION}:${ACCOUNT_ID}:userpool/${POOL_ID}" \
  --identity-source "method.request.header.Authorization" \
  --region "$AWS_REGION" \
  --query "id" \
  --output text 2>/dev/null || \
  aws apigateway get-authorizers \
    --rest-api-id "$API_GW_ID" \
    --region "$AWS_REGION" \
    --query "items[?name=='sled-mcp-cognito-authorizer'].id" \
    --output text)

echo "      Authorizer ID: $AUTHORIZER_ID"

# PUT method with Cognito authorizer for /mcp
for METHOD in POST GET; do
  aws apigateway put-method \
    --rest-api-id "$API_GW_ID" \
    --resource-id "$MCP_RESOURCE_ID" \
    --http-method "$METHOD" \
    --authorization-type COGNITO_USER_POOLS \
    --authorizer-id "$AUTHORIZER_ID" \
    --authorization-scopes "$COGNITO_SCOPE" \
    --region "$AWS_REGION" > /dev/null 2>/dev/null || true

  # Lambda integration
  aws apigateway put-integration \
    --rest-api-id "$API_GW_ID" \
    --resource-id "$MCP_RESOURCE_ID" \
    --http-method "$METHOD" \
    --type AWS_PROXY \
    --integration-http-method POST \
    --uri "arn:aws:apigateway:${AWS_REGION}:lambda:path/2015-03-31/functions/${LAMBDA_ARN}/invocations" \
    --region "$AWS_REGION" > /dev/null 2>/dev/null || true
done

# PUT method for /.well-known/oauth-authorization-server (no auth required for discovery)
aws apigateway put-method \
  --rest-api-id "$API_GW_ID" \
  --resource-id "$OAUTH_RESOURCE_ID" \
  --http-method GET \
  --authorization-type NONE \
  --region "$AWS_REGION" > /dev/null 2>/dev/null || true

# Lambda integration for .well-known
aws apigateway put-integration \
  --rest-api-id "$API_GW_ID" \
  --resource-id "$OAUTH_RESOURCE_ID" \
  --http-method GET \
  --type AWS_PROXY \
  --integration-http-method POST \
  --uri "arn:aws:apigateway:${AWS_REGION}:lambda:path/2015-03-31/functions/${LAMBDA_ARN}/invocations" \
  --region "$AWS_REGION" > /dev/null 2>/dev/null || true

# Lambda permissions for API Gateway
aws lambda add-permission \
  --function-name "$LAMBDA_NAME" \
  --statement-id "apigateway-mcp-invoke" \
  --action "lambda:InvokeFunction" \
  --principal "apigateway.amazonaws.com" \
  --source-arn "arn:aws:execute-api:${AWS_REGION}:${ACCOUNT_ID}:${API_GW_ID}/*/*/mcp" \
  --region "$AWS_REGION" > /dev/null 2>/dev/null || true

aws lambda add-permission \
  --function-name "$LAMBDA_NAME" \
  --statement-id "apigateway-wellknown-invoke" \
  --action "lambda:InvokeFunction" \
  --principal "apigateway.amazonaws.com" \
  --source-arn "arn:aws:execute-api:${AWS_REGION}:${ACCOUNT_ID}:${API_GW_ID}/*/*/*" \
  --region "$AWS_REGION" > /dev/null 2>/dev/null || true

# Deploy to stage
aws apigateway create-deployment \
  --rest-api-id "$API_GW_ID" \
  --stage-name "$STAGE" \
  --region "$AWS_REGION" > /dev/null

MCP_URL="https://${API_GW_ID}.execute-api.${AWS_REGION}.amazonaws.com/${STAGE}/mcp"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "============================================="
echo " ✅ Deploy Complete"
echo "============================================="
echo ""
echo " MCP Endpoint  : $MCP_URL"
echo " Token URL     : $TOKEN_URL"
echo " Cognito Pool  : $POOL_ID"
echo " Client ID     : $CLIENT_ID"
echo " Client Secret : $CLIENT_SECRET"
echo " Scope         : $COGNITO_SCOPE"
echo ""
echo "─────────────────────────────────────────────"
echo " Otto MCP Config (paste into Otto)"
echo "─────────────────────────────────────────────"
cat <<EOF
{
  "mcpServers": {
    "sled-competitive-intel": {
      "url": "$MCP_URL",
      "auth": {
        "type": "oauth2",
        "grant_type": "client_credentials",
        "token_url": "$TOKEN_URL",
        "client_id": "$CLIENT_ID",
        "client_secret": "$CLIENT_SECRET",
        "scope": "$COGNITO_SCOPE"
      }
    }
  }
}
EOF
echo ""
echo " Test token fetch:"
echo "   curl -X POST $TOKEN_URL \\"
echo "     -H 'Content-Type: application/x-www-form-urlencoded' \\"
echo "     -d 'grant_type=client_credentials&client_id=$CLIENT_ID&client_secret=$CLIENT_SECRET&scope=$COGNITO_SCOPE'"
echo ""
echo " Test MCP initialize (replace TOKEN):"
echo "   curl -X POST $MCP_URL \\"
echo "     -H 'Authorization: Bearer TOKEN' \\"
echo "     -H 'Content-Type: application/json' \\"
echo "     -d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{}}'"
