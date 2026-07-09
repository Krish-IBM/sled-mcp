# Integrating IBM W3 SSO with SLED MCP Server

## Overview

IBM W3 SSO (OpenID Connect) can replace AWS Cognito for authentication. This provides enterprise-grade SSO with your IBM credentials.

**IBM W3 SSO Details:**
- **Issuer:** `https://login.w3.ibm.com/oidc/endpoint/default`
- **Token Endpoint:** `https://login.w3.ibm.com/v1.0/endpoint/default/token`
- **Discovery:** `https://login.w3.ibm.com/oidc/endpoint/default/.well-known/openid-configuration`
- **Supports:** `client_credentials` grant type ✅

---

## Integration Options

### Option A: API Gateway JWT Authorizer (Recommended)

Configure API Gateway to validate IBM W3 SSO JWT tokens directly.

**Pros:**
- No code changes needed in Lambda
- API Gateway handles all authentication
- Better security (tokens never reach Lambda)
- Automatic token validation

**Cons:**
- Requires API Gateway configuration
- Need to register OAuth client in IBM W3 SSO

**Steps:**

1. **Register OAuth Client in IBM W3 SSO**
   - Go to IBM Cloud Identity Management
   - Create new OAuth 2.0 client application
   - Grant type: `client_credentials`
   - Get `client_id` and `client_secret`
   - Configure scopes (e.g., `openid`, custom scopes)

2. **Update API Gateway Authorizer**
   ```bash
   # Delete existing Cognito authorizer
   aws apigateway delete-authorizer \
     --rest-api-id zie08z9fuj \
     --authorizer-id <COGNITO_AUTHORIZER_ID> \
     --region us-east-1
   
   # Create JWT authorizer for IBM W3 SSO
   aws apigateway create-authorizer \
     --rest-api-id zie08z9fuj \
     --name "ibm-w3-sso-authorizer" \
     --type JWT \
     --identity-source '$request.header.Authorization' \
     --jwt-configuration issuer=https://login.w3.ibm.com/oidc/endpoint/default,audience=<YOUR_CLIENT_ID> \
     --region us-east-1
   ```

3. **Update /mcp Method to Use New Authorizer**
   ```bash
   aws apigateway update-method \
     --rest-api-id zie08z9fuj \
     --resource-id <MCP_RESOURCE_ID> \
     --http-method POST \
     --patch-operations op=replace,path=/authorizationType,value=JWT \
     --region us-east-1
   ```

4. **Deploy Changes**
   ```bash
   aws apigateway create-deployment \
     --rest-api-id zie08z9fuj \
     --stage-name sled-docs-query \
     --region us-east-1
   ```

---

### Option B: Lambda-Based Authentication (Simpler Setup)

Handle IBM W3 SSO token validation in Lambda code.

**Pros:**
- No API Gateway changes needed
- More flexible (can add custom logic)
- Easier to debug

**Cons:**
- Lambda must validate tokens
- Slightly higher latency
- Need to implement JWT validation

**Implementation:**

I'll create a new Lambda handler that validates IBM W3 SSO tokens.

---

## Recommended Approach

**Use Option A (API Gateway JWT Authorizer)** because:
1. Better security architecture
2. Automatic token validation
3. No Lambda code changes
4. Industry best practice

---

## Prerequisites

Before proceeding, you need:

1. **IBM W3 SSO OAuth Client Registration**
   - Client ID
   - Client Secret
   - Configured scopes
   - Redirect URIs (if needed)

2. **Permissions**
   - Access to IBM Cloud Identity Management
   - Ability to create OAuth applications

---

## Step-by-Step Setup Script

I'll create a deployment script that:
1. Removes Cognito authorizer
2. Configures IBM W3 SSO JWT authorizer
3. Updates API Gateway methods
4. Deploys changes
5. Provides Otto configuration

---

## Otto Configuration (After Setup)

```json
{
  "mcpServers": {
    "sled-competitive-intel": {
      "command": "npx",
      "args": [
        "-y",
        "@modelcontextprotocol/server-http-client",
        "https://zie08z9fuj.execute-api.us-east-1.amazonaws.com/sled-docs-query/mcp"
      ],
      "env": {
        "MCP_HTTP_AUTH_TYPE": "bearer",
        "MCP_HTTP_AUTH_TOKEN_URL": "https://login.w3.ibm.com/v1.0/endpoint/default/token",
        "MCP_HTTP_AUTH_CLIENT_ID": "<YOUR_IBM_CLIENT_ID>",
        "MCP_HTTP_AUTH_CLIENT_SECRET": "<YOUR_IBM_CLIENT_SECRET>",
        "MCP_HTTP_AUTH_SCOPE": "openid"
      }
    }
  }
}
```

---

## Testing IBM W3 SSO Authentication

### Get Access Token:
```bash
curl -X POST https://login.w3.ibm.com/v1.0/endpoint/default/token \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -d 'grant_type=client_credentials' \
  -d 'client_id=YOUR_CLIENT_ID' \
  -d 'client_secret=YOUR_CLIENT_SECRET' \
  -d 'scope=openid'
```

### Test MCP Endpoint:
```bash
curl -X POST https://zie08z9fuj.execute-api.us-east-1.amazonaws.com/sled-docs-query/mcp \
  -H 'Authorization: Bearer YOUR_IBM_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
```

---

## Next Steps

1. **Do you have IBM W3 SSO OAuth client credentials?**
   - If YES: I'll create the deployment script
   - If NO: I'll guide you through registration

2. **Which option do you prefer?**
   - Option A: API Gateway JWT Authorizer (recommended)
   - Option B: Lambda-based validation (simpler)

Let me know and I'll proceed with the implementation!