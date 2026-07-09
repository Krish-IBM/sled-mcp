# One-time AWS admin setup for the SLED scoring agent

The scoring Lambda needs an execution role with **S3 + Bedrock + Textract +
self-invoke + logs**. In account `211125468742` (us-east-1), the user
`Krish.Chavan@ibm.com` cannot provision it.

## Why the user is blocked (root cause)

The `co-op_developers` group's inline policy `IAMSpecificPolicy` lists
`iam:CreateRole` / `iam:PutRolePolicy` / `iam:AttachRolePolicy` but scopes them to
`Resource: arn:aws:iam::*:user/${aws:username}`. Those actions operate on a
**role** ARN, which never matches a **user** ARN — so the grant is ineffective
(AWS returns "no identity-based policy allows iam:CreateRole"). `iam:PassRole`
(required to attach any role to a Lambda) is not granted at all. The user also
lacks `s3:PutBucketPolicy`. Pick Option A (fastest) or Option C (enables the user
to self-serve going forward).

## Option A (recommended): create a dedicated role

**All 3 commands below can be pasted directly into AWS CloudShell — no files needed.**

Open CloudShell: AWS Console → top nav → CloudShell icon (>_). Then paste each block:

```bash
aws iam create-role \
  --role-name sled-scoring-agent-role \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
```

```bash
aws iam put-role-policy \
  --role-name sled-scoring-agent-role \
  --policy-name sled-scoring-agent-policy \
  --policy-document '{"Version":"2012-10-17","Statement":[{"Sid":"Logs","Effect":"Allow","Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],"Resource":"*"},{"Sid":"S3","Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:ListBucket"],"Resource":["arn:aws:s3:::sled-scoring-agent-bucket","arn:aws:s3:::sled-scoring-agent-bucket/*"]},{"Sid":"Bedrock","Effect":"Allow","Action":["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream"],"Resource":"*"},{"Sid":"Textract","Effect":"Allow","Action":["textract:StartDocumentTextDetection","textract:GetDocumentTextDetection"],"Resource":"*"},{"Sid":"SelfInvoke","Effect":"Allow","Action":["lambda:InvokeFunction"],"Resource":"arn:aws:lambda:us-east-1:211125468742:function:sled-scoring-agent"}]}'
```

```bash
aws iam put-user-policy \
  --user-name Krish.Chavan@ibm.com \
  --policy-name sled-scoring-passrole \
  --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"iam:PassRole","Resource":"arn:aws:iam::211125468742:role/sled-scoring-agent-role"}]}'
```

Then send Krish the role ARN:
`arn:aws:iam::211125468742:role/sled-scoring-agent-role`

He finishes the deploy with:

```bash
ROLE_ARN=arn:aws:iam::211125468742:role/sled-scoring-agent-role \
SCORING_BUCKET=sled-scoring-agent-bucket \
./deploy_scoring.sh
```
_(Model defaults to `us.anthropic.claude-sonnet-4-20250514-v1:0` — confirmed working.)_

## Option B: grant Krish the IAM permissions, he self-provisions

Attach to user `Krish.Chavan@ibm.com` (optionally scoped/​boundaried):
`iam:CreateRole`, `iam:PutRolePolicy`, `iam:PassRole` (on
`role/sled-scoring-agent-role`). Then `./deploy_scoring.sh` (no `ROLE_ARN`)
creates the role itself.

## Option C: fix the co-op group so the user can self-serve

Add a correctly-scoped statement (scoped to `sled-*` roles) to the
`co-op_developers` group — this fixes the resource-scoping bug and adds
`PassRole`:

```bash
aws iam put-group-policy --group-name co-op_developers \
  --policy-name SledRoleManagement \
  --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
    "Action":["iam:CreateRole","iam:PutRolePolicy","iam:AttachRolePolicy",
              "iam:DetachRolePolicy","iam:DeleteRolePolicy","iam:GetRole","iam:PassRole"],
    "Resource":"arn:aws:iam::211125468742:role/sled-*"}]}'
```

Then the user runs `./deploy_scoring.sh` (no `ROLE_ARN`) and it self-provisions
`sled-scoring-agent-role`.

## Also required: Bedrock model access

Invoking the chosen Claude models fails with *"Model access is denied ... AWS
Marketplace actions (aws-marketplace:ViewSubscriptions, aws-marketplace:Subscribe)"*.
The account's Bedrock **model access** must be enabled for the models we use:

* In the Bedrock console → **Model access**, enable the desired Anthropic models
  (the user wants `claude-opus-4-7` and `claude-sonnet-5`; **neither is currently
  accessible**).
* Ensure the invoking principal (the Lambda execution role, and ideally the user
  for local testing) can perform `aws-marketplace:ViewSubscriptions` /
  `aws-marketplace:Subscribe`.

Confirmed working today without extra setup: `us.anthropic.claude-sonnet-4-20250514-v1:0`
(Sonnet 4). Until opus-4-7 access is enabled, deploy with:
`SCORING_MODEL_ID=us.anthropic.claude-sonnet-4-20250514-v1:0` and the same for
`SCORING_FAST_MODEL_ID`.

## Step 2: grant CI bucket read access (for `score deal=<id>`)

Run this in CloudShell to update the existing inline policy on `sled-scoring-agent-role`.
This adds read-only access to `competitive-intelligence-sled` — the Lambda can now stage
deal documents automatically without any manual uploads.

```bash
aws iam put-role-policy \
  --role-name sled-scoring-agent-role \
  --policy-name sled-scoring-agent-policy \
  --policy-document '{"Version":"2012-10-17","Statement":[{"Sid":"Logs","Effect":"Allow","Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],"Resource":"*"},{"Sid":"S3ProjectDocsAndOutputs","Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:ListBucket"],"Resource":["arn:aws:s3:::sled-scoring-agent-bucket","arn:aws:s3:::sled-scoring-agent-bucket/*"]},{"Sid":"S3CIBucketRead","Effect":"Allow","Action":["s3:GetObject","s3:ListBucket"],"Resource":["arn:aws:s3:::competitive-intelligence-sled","arn:aws:s3:::competitive-intelligence-sled/*"]},{"Sid":"BedrockClaude","Effect":"Allow","Action":["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream"],"Resource":"*"},{"Sid":"TextractOCR","Effect":"Allow","Action":["textract:StartDocumentTextDetection","textract:GetDocumentTextDetection"],"Resource":"*"},{"Sid":"SelfInvokeForAsyncJobs","Effect":"Allow","Action":["lambda:InvokeFunction"],"Resource":"arn:aws:lambda:us-east-1:211125468742:function:sled-scoring-agent"}]}'
```

`put-role-policy` is idempotent — it replaces the existing policy document in-place.

## Already provisioned (no admin needed)
* S3 bucket `sled-scoring-agent-bucket` (+ `templates/scorecard.pptx`)
* Deployment package built & validated (Linux/cp312)
* Bedrock model access confirmed: `us.anthropic.claude-sonnet-4-20250514-v1:0`
