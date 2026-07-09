# Connecting SLED MCP Server to Otto

## Current Otto URL

Use the HTTP API front door, not the original REST API URL:

```text
https://5nlltkt7x7.execute-api.us-east-1.amazonaws.com/mcp
```

This endpoint is backed by the existing `sled-mcp-server` Lambda and leaves the existing REST API `zie08z9fuj` routes untouched.

## Otto Connector Settings

- Name: `SLED-SS`
- URL: `https://5nlltkt7x7.execute-api.us-east-1.amazonaws.com/mcp`
- Scopes: `openid`
- OAuth flow: authorization code with PKCE
- Client ID: use the full IBM w3id client ID from the provisioner
- Client Secret: paste only into Otto / secret storage; do not commit it

## Why This URL

The original REST API URL under `zie08z9fuj` has two problems for Otto discovery:

- Raw REST API URLs require a stage path such as `/prod`.
- API Gateway REST remaps the required `WWW-Authenticate` response header to `x-amzn-remapped-www-authenticate`.

The HTTP API front door avoids both issues. It has a root URL with no stage and preserves the real `www-authenticate` header.

## Verified Discovery Endpoints

```text
GET https://5nlltkt7x7.execute-api.us-east-1.amazonaws.com/mcp
```

Expected:

```text
HTTP/2 401
www-authenticate: Bearer realm="mcp", resource_metadata="https://5nlltkt7x7.execute-api.us-east-1.amazonaws.com/.well-known/oauth-protected-resource"
```

```text
GET https://5nlltkt7x7.execute-api.us-east-1.amazonaws.com/.well-known/oauth-protected-resource
```

Expected JSON:

```json
{
  "resource": "https://5nlltkt7x7.execute-api.us-east-1.amazonaws.com/mcp",
  "authorization_servers": ["https://5nlltkt7x7.execute-api.us-east-1.amazonaws.com"],
  "scopes_supported": ["openid"],
  "bearer_methods_supported": ["header"]
}
```

```text
GET https://5nlltkt7x7.execute-api.us-east-1.amazonaws.com/.well-known/oauth-authorization-server
GET https://5nlltkt7x7.execute-api.us-east-1.amazonaws.com/.well-known/openid-configuration
```

Both return authorization-server metadata with an `issuer` field and the w3id introspection endpoint.

## Required Introspection Setup

Otto is currently sending a bearer value that is not decodable as a JWT, so the Lambda must validate it through w3id introspection.

Run:

```bash
./configure_introspection.sh
```

When prompted, enter:

- Full w3id Client ID from the provisioner
- w3id Client Secret

The script stores the secret in AWS Secrets Manager, grants `sled-mcp-server-role` read access to only that secret, and updates the Lambda environment for introspection.

If your AWS user cannot create Secrets Manager secrets, ask an AWS admin to create a secret first.

Secret name:

```text
sled-mcp/w3id-client-secret
```

Secret value format:

```json
{"client_secret":"<w3id-client-secret>"}
```

Then rerun:

```bash
W3ID_CLIENT_SECRET_ARN=<secret-arn> ./configure_introspection.sh
```

The same script will skip secret creation and only wire Lambda/IAM to the existing secret.

Minimum AWS permissions needed by whoever runs the full setup:

```text
secretsmanager:CreateSecret
secretsmanager:PutSecretValue
secretsmanager:DescribeSecret
iam:PutRolePolicy on role sled-mcp-server-role
lambda:GetFunctionConfiguration on function sled-mcp-server
lambda:UpdateFunctionConfiguration on function sled-mcp-server
```

## Notes

- The Lambda validates w3id JWTs locally with JWKS, then falls back to w3id token introspection for non-JWT bearer tokens.
- Lambda env vars `W3ID_AUDIENCE` and `W3ID_CLIENT_ID` must be the full w3id Client ID / token audience value, not the truncated provisioner display.
- The API front door does not use a Gateway authorizer; Lambda owns the OAuth challenge so Otto can discover auth metadata.
- The old Cognito/client-credentials guidance is obsolete for the w3id Otto connector.
