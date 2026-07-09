#!/usr/bin/env bash
# Configure w3id token introspection for sled-mcp-server.

set -euo pipefail

AWS_REGION="us-east-1"
LAMBDA_NAME="sled-mcp-server"
ROLE_NAME="sled-mcp-server-role"
SECRET_NAME="sled-mcp/w3id-client-secret"
INTROSPECTION_URL="https://login.w3.ibm.com/v1.0/endpoint/default/introspect"
W3ID_CLIENT_SECRET_ARN="${W3ID_CLIENT_SECRET_ARN:-}"

read -r -p "Full w3id Client ID: " W3ID_CLIENT_ID

if [[ -z "$W3ID_CLIENT_ID" ]]; then
  echo "Client ID is required." >&2
  exit 1
fi

if [[ -z "$W3ID_CLIENT_SECRET_ARN" ]]; then
  read -r -s -p "w3id Client Secret: " W3ID_CLIENT_SECRET
  echo ""

  if [[ -z "$W3ID_CLIENT_SECRET" ]]; then
    echo "Client secret is required unless W3ID_CLIENT_SECRET_ARN is already set." >&2
    exit 1
  fi
fi

secret_file="$(mktemp /private/tmp/w3id-secret.XXXXXX.json)"
policy_file="$(mktemp /private/tmp/w3id-policy.XXXXXX.json)"
env_file="$(mktemp /private/tmp/w3id-env.XXXXXX.json)"
env_update_file="$(mktemp /private/tmp/w3id-env-update.XXXXXX.json)"
trap 'rm -f "$secret_file" "$policy_file" "$env_file" "$env_update_file"' EXIT

if [[ -z "$W3ID_CLIENT_SECRET_ARN" ]]; then
  chmod 600 "$secret_file"
  W3ID_CLIENT_SECRET_VALUE="$W3ID_CLIENT_SECRET" python3 - "$secret_file" <<'PY'
import json
import os
import sys

with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump({"client_secret": os.environ["W3ID_CLIENT_SECRET_VALUE"]}, f)
PY
  unset W3ID_CLIENT_SECRET W3ID_CLIENT_SECRET_VALUE

  if aws secretsmanager describe-secret \
    --secret-id "$SECRET_NAME" \
    --region "$AWS_REGION" >/dev/null 2>&1; then
    aws secretsmanager put-secret-value \
      --secret-id "$SECRET_NAME" \
      --secret-string "file://$secret_file" \
      --region "$AWS_REGION" >/dev/null
  else
    if ! aws secretsmanager create-secret \
      --name "$SECRET_NAME" \
      --secret-string "file://$secret_file" \
      --region "$AWS_REGION" >/dev/null; then
      cat >&2 <<MSG

Could not create Secrets Manager secret '$SECRET_NAME'.
Ask an AWS admin to create the secret, then rerun this script as:

  W3ID_CLIENT_SECRET_ARN=<secret-arn> ./configure_introspection.sh

The secret value can be either plain text or JSON:
  {"client_secret":"..."}

MSG
      exit 1
    fi
  fi

  SECRET_ARN="$(aws secretsmanager describe-secret \
    --secret-id "$SECRET_NAME" \
    --query ARN \
    --output text \
    --region "$AWS_REGION")"
else
  SECRET_ARN="$W3ID_CLIENT_SECRET_ARN"
  aws secretsmanager describe-secret \
    --secret-id "$SECRET_ARN" \
    --region "$AWS_REGION" >/dev/null
fi

python3 - "$SECRET_ARN" "$policy_file" <<'PY'
import json
import sys

secret_arn, policy_file = sys.argv[1], sys.argv[2]
policy = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": "secretsmanager:GetSecretValue",
            "Resource": secret_arn,
        }
    ],
}
with open(policy_file, "w", encoding="utf-8") as f:
    json.dump(policy, f)
PY

aws iam put-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name "sled-mcp-read-w3id-client-secret" \
  --policy-document "file://$policy_file" \
  --region "$AWS_REGION"

aws lambda get-function-configuration \
  --function-name "$LAMBDA_NAME" \
  --query "Environment.Variables" \
  --output json \
  --region "$AWS_REGION" > "$env_file"

python3 - "$env_file" "$env_update_file" "$W3ID_CLIENT_ID" "$SECRET_ARN" "$INTROSPECTION_URL" <<'PY'
import json
import sys

env_file, env_update_file, client_id, secret_arn, introspection_url = sys.argv[1:]
with open(env_file, "r", encoding="utf-8") as f:
    variables = json.load(f)

variables.update({
    "W3ID_AUDIENCE": client_id,
    "W3ID_CLIENT_ID": client_id,
    "W3ID_CLIENT_SECRET_ARN": secret_arn,
    "W3ID_INTROSPECTION_URL": introspection_url,
    "W3ID_SCOPES_SUPPORTED": "openid",
})

with open(env_update_file, "w", encoding="utf-8") as f:
    json.dump({"Variables": variables}, f)
PY

aws lambda update-function-configuration \
  --function-name "$LAMBDA_NAME" \
  --environment "file://$env_update_file" \
  --region "$AWS_REGION" >/dev/null

aws lambda wait function-updated \
  --function-name "$LAMBDA_NAME" \
  --region "$AWS_REGION"

echo "Configured introspection for $LAMBDA_NAME."
echo "Secret ARN: $SECRET_ARN"
