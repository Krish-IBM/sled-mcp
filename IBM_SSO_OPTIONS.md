# IBM W3 SSO Integration - Grant Type Issue & Solutions

## Problem Identified

Your IBM W3 SSO OAuth client credentials:
- **Client ID:** `N2FiODhhMTQtOGUxMS00`
- **Supported Grant Types:** `authorization_code`, `implicit` only
- **Missing:** `client_credentials` grant type

**Error received:**
```
CSIAQ0172E The grant type [client_credentials] is not supported.
Supported grant types are [authorization_code, implicit].
```

This OAuth client is configured for **user authentication** (interactive login), not **machine-to-machine** authentication that Otto needs.

---

## Solution Options

### Option 1: Request New OAuth Client (Recommended)

**What:** Get a new IBM W3 SSO OAuth client configured with `client_credentials` grant type.

**Steps:**
1. Contact your IBM Cloud Identity administrator
2. Request a new OAuth 2.0 client application with:
   - Grant type: `client_credentials`
   - Token endpoint auth method: `client_secret_post` or `client_secret_basic`
   - Scopes: `openid` (or custom scopes for your app)
3. Once you receive new credentials, use them with Option A setup

**Pros:**
- Proper machine-to-machine authentication
- No user interaction needed
- Works seamlessly with Otto
- Industry best practice

**Cons:**
- Requires admin approval
- May take time to provision

**Timeline:** Usually 1-3 business days

---

### Option 2: Use Authorization Code Flow with Device Code

**What:** Use the `authorization_code` grant type with device code flow for initial authentication.

**How it works:**
1. Otto initiates device code flow
2. User visits URL and enters code
3. User authenticates with IBM W3 SSO
4. Otto receives access token
5. Token is cached and refreshed automatically

**Pros:**
- Works with your existing OAuth client
- No new credentials needed
- User authenticates with their IBM credentials

**Cons:**
- Requires one-time user interaction
- More complex setup
- Token refresh needed

**Otto Configuration:**
```json
{
  "mcpServers": {
    "sled-competitive-intel": {
      "url": "https://zie08z9fuj.execute-api.us-east-1.amazonaws.com/sled-docs-query/mcp",
      "auth": {
        "type": "oauth2",
        "grant_type": "authorization_code",
        "authorization_url": "https://login.w3.ibm.com/v1.0/endpoint/default/authorize",
        "token_url": "https://login.w3.ibm.com/v1.0/endpoint/default/token",
        "client_id": "N2FiODhhMTQtOGUxMS00",
        "client_secret": "YjMyY2YwZTEtYjU4My00",
        "scope": "openid",
        "redirect_uri": "http://localhost:8080/callback"
      }
    }
  }
}
```

---

### Option 3: Hybrid Approach (Quick Start)

**What:** Keep AWS Cognito for Otto (machine-to-machine), add IBM SSO for user-facing features later.

**Why:**
- Your Cognito setup already works
- No waiting for new IBM credentials
- Can add IBM SSO later for user authentication

**Current Working Setup:**
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
        "MCP_HTTP_AUTH_TOKEN_URL": "https://sled-mcp-468742.auth.us-east-1.amazoncognito.com/oauth2/token",
        "MCP_HTTP_AUTH_CLIENT_ID": "p37tqktdvfq1qrs854s8bju92",
        "MCP_HTTP_AUTH_CLIENT_SECRET": "1l4ljg1t20rn8eaotgh2pip3fsl3u3nmj4e8ndpl6il4vrsbv8qm",
        "MCP_HTTP_AUTH_SCOPE": "https://sled-mcp-api/invoke"
      }
    }
  }
}
```

**Pros:**
- Works immediately
- No changes needed
- Can migrate to IBM SSO later

**Cons:**
- Not using IBM SSO (yet)
- Two auth systems to manage

---

### Option 4: API Key Authentication (Simplest)

**What:** Use a simple API key instead of OAuth for Otto.

**Implementation:**
1. Generate a secure API key
2. Store it in AWS Secrets Manager
3. Lambda validates API key from request header
4. No OAuth complexity

**Otto Configuration:**
```json
{
  "mcpServers": {
    "sled-competitive-intel": {
      "url": "https://zie08z9fuj.execute-api.us-east-1.amazonaws.com/sled-docs-query/mcp",
      "headers": {
        "X-API-Key": "your-secure-api-key-here"
      }
    }
  }
}
```

**Pros:**
- Simplest setup
- No OAuth complexity
- Works immediately
- Easy to rotate keys

**Cons:**
- Less secure than OAuth
- No token expiration
- Manual key management

---

## Recommendation

**For immediate use:** Try **Option 3** (keep Cognito) with the MCP HTTP client proxy configuration from OTTO_CONNECTION_GUIDE.md. This should work right now.

**For production:** Request **Option 1** (new IBM OAuth client with client_credentials) from your IBM Cloud Identity admin.

**For testing:** If Option 3 doesn't work due to the stage URL issue, implement **Option 4** (API Key) as a quick workaround.

---

## Next Steps

**Tell me which option you prefer:**

1. **Option 1** - I'll help you draft the request for IBM Cloud Identity admin
2. **Option 2** - I'll implement authorization code flow with device code
3. **Option 3** - Let's test the Cognito setup with MCP HTTP client proxy
4. **Option 4** - I'll implement API key authentication

Which would you like to proceed with?