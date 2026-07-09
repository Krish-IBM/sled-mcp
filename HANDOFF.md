# SLED Scoring Agent — Handoff Document

## What This Is

A bid-scoring backend deployed as an AWS Lambda (`sled-scoring-agent`) behind the existing
MCP router Lambda (`sled-mcp-server`). It evaluates competing vendor proposals on government
procurements through a competitive-intelligence lens (focal vendor = IBM). Outputs: JSON +
Excel + PowerPoint scorecard.

The MCP router (`lambda_handler.py`) already had a `SCORING_AGENT_URL` slot and a
`"scoring: <query>"` routing path. This project fills that slot.

---

## AWS Environment

| Resource | Value |
|---|---|
| Account | 211125468742 |
| Region | us-east-1 |
| Scoring Lambda | `sled-scoring-agent` |
| MCP Router Lambda | `sled-mcp-server` |
| Execution Role | `arn:aws:iam::211125468742:role/sled-scoring-agent-role` |
| Scoring / output bucket | `sled-scoring-agent-bucket` |
| CI source bucket | `competitive-intelligence-sled` |
| Bedrock model (strong + fast) | `us.anthropic.claude-sonnet-4-20250514-v1:0` |
| Lambda memory | **4096 MB** (bumped from 2048 on 2026-07-07 for OCR throughput) |

**Claude model note:** opus-4-7 and sonnet-5 are not enabled in this account
(AccessDenied at Marketplace subscription). Sonnet 4 confirmed working.

---

## How Queries Flow

```
Otto → @SLED-MCP scoring: score deal="Accenture/AZ AFIS (ERP) (2013)" focal=IBM
  → sled-mcp-server (MCP router Lambda, unchanged)
    → POST {"query": "..."} to SCORING_AGENT_URL (29s cap)
      → sled-scoring-agent Lambda (handler.py)
          verb=score  → write job JSON to S3, self-invoke async, return {job_id}
          verb=status → read jobs/<job_id>.json from S3 (stalled-job detection)
          verb=result → read job + presign output URLs
              ↓ (async worker)
          retry-noop check → bail if already attempted (prevents Lambda auto-retry churn)
          CI staging gate → list+classify+copy files from competitive-intelligence-sled
          run_scoring() → ingest → scheme → extract → generate → aggregate → [deck-content] → render
          Upload scorecard.json / .xlsx to sled-scoring-agent-bucket/outputs/
```

**29-second hard timeout** from the MCP router means the async job model is mandatory —
`score` returns a `job_id` immediately; results are polled with `status` / `result`.

### Agent routing (MCP router → agent Lambda)

The router (`lambda_handler.py`) exposes **one MCP tool per configured agent**
(`sled_scoring`, `sled_docs`, `sled_deal`) **plus a `sled_agent` router tool**:

- `sled_scoring` / `sled_docs` / `sled_deal` — the tool name *is* the agent; the whole
  query is forwarded to that backend (`AGENT_REGISTRY[name]["payload_key"]`).
- `sled_agent` — for when the agent is unspecified. An explicit `agent:` prefix still
  wins (`scoring: ...`); otherwise `choose_agent()` picks one by a keyword heuristic
  over `AGENT_REGISTRY[*]["keywords"]`, falling back to `DEFAULT_AGENT` (env, default
  `docs`). Agents with no backend URL are neither advertised nor auto-selected.

To add an agent: add a registry entry (url + payload_key + description + keywords) and
set its env var. Tools and routing pick it up automatically.

---

## Command Grammar (in Otto)

```
@SLED-MCP scoring: score deal=<company> [focal=IBM]
    # Stages entire company folder — will fast-fail if >60 docs / 400 MB

@SLED-MCP scoring: score deal="<Company>/<Subfolder>" [focal=IBM]
    # CORRECT form: scope to ONE procurement inside a company folder
    # Example: deal="Accenture/AZ AFIS (ERP) (2013)"
    # Quotes required when the path has spaces or slashes

@SLED-MCP scoring: score project=<id> [focal=IBM]
    # Score a project already uploaded to sled-scoring-agent-bucket/projects/<id>/

@SLED-MCP scoring: status <job_id>
@SLED-MCP scoring: result <job_id>
```

**Critical:** User must include `@SLED-MCP` in every Otto message — Otto drops the tool
registration between turns and the `@` mention re-registers it.

**Sub-procurement path → staging slug:** `deal="Accenture/AZ AFIS (ERP) (2013)"` stages
to `projects/Accenture_AZ_AFIS_ERP_2013/` (special chars → underscores). The job record
stores both `ci_path` (original) and `project_id` (slug).

---

## CI Bucket Layout — IMPORTANT: Organized by Company, NOT Procurement

`competitive-intelligence-sled` has **company folders at the root** — each company folder
contains **many unrelated procurements**:

```
competitive-intelligence-sled/
  Accenture/
    AZ AFIS (ERP) (2013)/      ← one procurement
    CA CDCR S4 HANA Migration 2024 FOIA/
    Accenture FL CCWIS 2022 Proposal/
    Accenture Santa Clara ERP Proposal/
    ... (~40 sub-folders, unrelated procurements)
  Deloitte/
  CGI/
  ...
```

**Do NOT use `deal=<company>` alone** — it stages the entire company corpus (~100+ files,
900 MB) and will hit the input-size guardrail fast-fail. Always scope to one sub-folder:
`deal="<Company>/<Subfolder>"`.

A **competitive scorecard** (multiple vendors vs. one RFP) is not possible from this bucket
alone — each company folder only has that company's docs. A true competitive run requires a
matched project set uploaded to `projects/<id>/` (see Pending #2 below).

`CI_DEALS_PREFIX` is intentionally empty (Lambda env var not set, Python code defaults to `""`).

On first `score deal="X/Y"`, the async worker:
1. Lists all keys under `X/Y/` in `competitive-intelligence-sled`
2. Classifies them (heuristics first; `default_vendor=X` so all unmatched files → company proposal, not pseudo-vendors)
3. Copies them server-side to `sled-scoring-agent-bucket/projects/<slug>/rfp|proposals/<Vendor>|scoresheet/`
4. Writes `projects/<slug>/.ci_manifest.json` — subsequent runs skip staging entirely

---

## File Structure

```
sled_mcp_folder/
├── lambda_handler.py              # MCP router — per-agent tools + keyword auto-routing (see "Agent routing")
│                                  # AGENT_REGISTRY drives tools/list, prefix parsing, and choose_agent()
├── scoring_agent/
│   ├── __init__.py
│   ├── handler.py                 # Lambda entry: parse verbs, job lifecycle, staging gate, retry-noop
│   ├── pipeline.py                # Orchestrates pipeline; input-size guardrail; deadline budget checks
│   ├── models.py                  # Dataclasses: SchemeSpec, ScoreCell, VendorResult, ScorecardResult
│   ├── ingest.py                  # PDF text extraction; deadline-aware OCR; skips .zip/.tif/.doc/.xls
│   ├── stage_from_ci.py           # list/classify/copy CI bucket deals to staging bucket
│   ├── rfp_scheme.py              # Parse RFP → SchemeSpec (Claude)
│   ├── extract_scores.py          # Extract real scores from FOIA scoresheets (Claude)
│   ├── score_generate.py          # Generate predicted scores from proposals (Claude)
│   ├── aggregate.py               # Totals, ranking, IBM CI insights + technical/financial split
│   ├── bedrock.py                 # Bedrock Converse API wrapper; bounded timeouts via botocore Config
│   ├── deck_content.py            # Bedrock: procurement metadata + Why-Won/Lost + category comparison (fails soft)
│   ├── render_json.py
│   ├── render_excel.py
│   ├── render_pptx.py             # 7-slide IBM-branded competitive deck (programmatic, no template needed)
│   ├── rubric/
│   │   ├── base_rubric.yaml       # 11-dimension standard SLED rubric (fallback when no RFP text)
│   │   └── __init__.py
│   └── requirements-scoring.txt
├── iam/
│   ├── scoring-agent-trust-policy.json
│   ├── scoring-agent-permissions-policy.json
│   └── ADMIN_SETUP.md             # CloudShell commands for admins — keep updated
├── tests/
│   ├── test_offline.py            # 5 tests, all pass (no AWS needed)
│   └── test_pipeline_mock.py      # Full pipeline with MockBedrock, passes
├── OTTO_MCP_DIAGNOSIS.md          # NEW: evidence + guidance for Otto "tool not found" bug
├── build_scoring_package.sh       # Docker-free zip builder (manylinux2014 wheels)
└── deploy_scoring.sh              # Creates/updates Lambda + API GW + wires SCORING_AGENT_URL
```

---

## Key Lambda Environment Variables

| Var | Value | Notes |
|---|---|---|
| `SCORING_BUCKET` | `sled-scoring-agent-bucket` | Input + output bucket |
| `SCORING_OUTPUT_BUCKET` | `sled-scoring-agent-bucket` | Same bucket |
| `SCORING_MODEL_ID` | `us.anthropic.claude-sonnet-4-20250514-v1:0` | Strong model |
| `SCORING_FAST_MODEL_ID` | `us.anthropic.claude-sonnet-4-20250514-v1:0` | Fast model |
| `BEDROCK_REGION` | `us-east-1` | |
| `DEFAULT_FOCAL` | `IBM` | |
| `SELF_FUNCTION_NAME` | `sled-scoring-agent` | For async self-invoke |
| `CI_BUCKET` | `competitive-intelligence-sled` | Source bucket for `deal=` |
| `CI_DEALS_PREFIX` | *(not set / empty)* | Deals are at bucket root |
| `SCORING_MAX_DOCS` | *(not set, default 60)* | Fast-fail if project has > N docs |
| `SCORING_MAX_TOTAL_MB` | *(not set, default 400)* | Fast-fail if total input > N MB |
| `SCORING_MAX_OCR_PAGES_PER_DOC` | *(not set, default 60)* | Page cap per scanned PDF |
| `RENDER_MARGIN_SECONDS` | *(not set, default 300)* | Reserve at end of Lambda budget for render |
| `BEDROCK_MAX_RETRIES` | *(not set, default 3)* | Throttle retry cap |
| `BEDROCK_CONNECT_TIMEOUT` | *(not set, default 10)* | Bedrock connection timeout (s) |
| `BEDROCK_READ_TIMEOUT` | *(not set, default 60)* | Bedrock read timeout per call (s) |
| `SCORING_PPTX_ENABLED` | *(not set)* | **Set to `1` to enable the 7-slide PowerPoint deck** (adds ~3 Bedrock calls/run) |
| `SCORING_PPTX_TEMPLATE_S3` | *(not set)* | Legacy: also enables PPTX if set. The deck now renders programmatically, so no template file is required. |

---

## Deploy Command

```bash
ROLE_ARN=arn:aws:iam::211125468742:role/sled-scoring-agent-role \
SCORING_BUCKET=sled-scoring-agent-bucket \
./deploy_scoring.sh
```

The script builds the zip, updates Lambda code + config, and wires `SCORING_AGENT_URL`
into `sled-mcp-server` automatically.

**Quick code-only redeploy** (no IAM/API changes, fastest path after Python edits):
```bash
rm -rf build/pkg/scoring_agent && cp -R scoring_agent build/pkg/scoring_agent
rm -f build/pkg/scoring_agent/requirements-scoring.txt
find build/pkg/scoring_agent -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
rm -f build/scoring_agent.zip
( cd build/pkg && zip -q -r -X ../scoring_agent.zip . )
aws lambda update-function-code --function-name sled-scoring-agent --region us-east-1 \
  --zip-file fileb://build/scoring_agent.zip --publish
aws lambda wait function-updated --function-name sled-scoring-agent --region us-east-1
```

---

## Admin CloudShell Commands (history)

These have already been run. Documented here in case the role needs to be recreated.

**Create role + attach policy:**
```bash
aws iam create-role --role-name sled-scoring-agent-role \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'

aws iam put-role-policy --role-name sled-scoring-agent-role \
  --policy-name sled-scoring-agent-policy \
  --policy-document '{"Version":"2012-10-17","Statement":[{"Sid":"Logs","Effect":"Allow","Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],"Resource":"*"},{"Sid":"S3ProjectDocsAndOutputs","Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:ListBucket"],"Resource":["arn:aws:s3:::sled-scoring-agent-bucket","arn:aws:s3:::sled-scoring-agent-bucket/*"]},{"Sid":"S3CIBucketRead","Effect":"Allow","Action":["s3:GetObject","s3:ListBucket"],"Resource":["arn:aws:s3:::competitive-intelligence-sled","arn:aws:s3:::competitive-intelligence-sled/*"]},{"Sid":"BedrockClaude","Effect":"Allow","Action":["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream"],"Resource":"*"},{"Sid":"TextractOCR","Effect":"Allow","Action":["textract:StartDocumentTextDetection","textract:GetDocumentTextDetection"],"Resource":"*"},{"Sid":"SelfInvokeForAsyncJobs","Effect":"Allow","Action":["lambda:InvokeFunction"],"Resource":"arn:aws:lambda:us-east-1:211125468742:function:sled-scoring-agent"}]}'

aws iam put-user-policy --user-name Krish.Chavan@ibm.com \
  --policy-name sled-scoring-passrole \
  --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"iam:PassRole","Resource":"arn:aws:iam::211125468742:role/sled-scoring-agent-role"}]}'
```

**Grant Lambda deploy permissions (already run):**
```bash
aws iam put-user-policy --user-name Krish.Chavan@ibm.com \
  --policy-name sled-scoring-deploy \
  --policy-document '{"Version":"2012-10-17","Statement":[{"Sid":"Lambda","Effect":"Allow","Action":["lambda:CreateFunction","lambda:UpdateFunctionCode","lambda:UpdateFunctionConfiguration","lambda:GetFunction","lambda:GetFunctionConfiguration","lambda:AddPermission","lambda:InvokeFunction","lambda:PublishVersion"],"Resource":["arn:aws:lambda:us-east-1:211125468742:function:sled-scoring-agent","arn:aws:lambda:us-east-1:211125468742:function:sled-mcp-server"]},{"Sid":"ApiGateway","Effect":"Allow","Action":["apigatewayv2:GetApis","apigatewayv2:CreateApi"],"Resource":"*"}]}'
```

**Admin-only: disable Lambda async auto-retry** (Krish is DENIED `PutFunctionEventInvokeConfig` — needs admin):
```bash
aws lambda put-function-event-invoke-config --function-name sled-scoring-agent \
  --region us-east-1 --maximum-retry-attempts 0 --maximum-event-age-in-seconds 1800
```
Until an admin runs this, the code-level retry-noop in `_run_job` prevents churn (reads live S3 record, bails if `attempts >= 1` or already `done/error`).

---

## Bugs Fixed (don't reintroduce)

| Bug | File | Fix |
|---|---|---|
| `temperature` field rejected by Claude 4.x on Bedrock | `bedrock.py` | `temperature` is `Optional[float] = None`, omitted from inferenceConfig unless explicitly set |
| `CI_DEALS_PREFIX` defaulted to `"deals/"` but bucket has folders at root | `handler.py` | Default changed to `""` with conditional suffix logic |
| Empty `CI_DEALS_PREFIX` caused AWS CLI parse error | `deploy_scoring.sh` | Only appended to ENV_VARS when non-empty |
| Trailing slash in `deal=Accenture/` caused double-slash path | `handler.py` | `.strip("/")` applied to parsed deal ID |
| `list_ci_deal_files` built invalid prefix when `deals_prefix` was empty | `stage_from_ci.py` | `parts = [p for p in [prefix.strip("/"), id.strip("/")] if p]` |
| OCR sequential → jobs stuck in ingest for 20+ minutes | `ingest.py`, `pipeline.py` | Parallel `_texts()` (ThreadPoolExecutor, 5 workers) + parallel OCR chunks (4 workers) |
| pdfminer color warnings flooded CloudWatch | `ingest.py` | `logging.getLogger("pdfminer").setLevel(logging.ERROR)` |
| `deal=<company>` stages entire 900 MB company corpus → 900s timeout | `pipeline.py` | Input-size fast-fail: `SCORING_MAX_DOCS=60`, `SCORING_MAX_TOTAL_MB=400` |
| Zombie jobs: timeout leaves job frozen at "running" forever | `handler.py` | `_is_stalled()` checks `worker_deadline_epoch`; `status`/`result` report stalled jobs as failed |
| Lambda auto-retry after timeout causes 3×900s churn | `handler.py` | Retry-noop: `_run_job` reads live S3 record at start, bails if `attempts >= 1` or `status in (done, error)` |
| Deal paths with spaces/slashes not parsed (e.g. `deal="Accenture/AZ AFIS"`) | `handler.py` | `_parse()` regex now accepts quoted values; `_slugify()` maps path → staging id |
| Whole-company staging fragments into 100+ pseudo-vendors | `stage_from_ci.py` | `default_vendor` hint passed to classifier; company name wins over flaky filename guessing |
| `_parse_iso()` applied local timezone to UTC timestamps | `handler.py` | Replaced `time.mktime()` with `calendar.timegm()` |
| OCR blocks entire Lambda budget (even on scoped folders) | `ingest.py` | `ocr_bedrock()` is deadline-aware: stops submitting chunks past budget, returns partial text; page cap 200→60/doc |
| Bedrock call timeout unbound → one hung call eats the budget | `bedrock.py` | `botocore.config.Config(connect_timeout=10, read_timeout=60)` applied at client creation |
| `.zip`, `.tif`, `.doc`, `.xls` fed to OCR path → noise/hang | `ingest.py` | Unsupported types skipped at `extract_text()` before any download |

---

## Otto / MCP Connector Notes

### "Tool not found" error (CollieError, stage 0)
This is an **Otto/Collie client-side bug**, not a server outage. CloudWatch shows the server returns `200` to every request. After a successful `tools/call`, Otto's client falls into an `initialize`-only loop (~every 30s) and never re-issues `tools/list`, so the tool disappears from its registry.

**Workaround:** Fully remove and re-add the SLED-MCP connector in Otto (don't just toggle).

**Server-side experiment (not yet deployed):** `lambda_handler.py` has a `MCP_STATEFUL_SESSIONS` env flag (default OFF). If set to `1`, the server returns an `MCP-Session-Id` on `initialize` responses — probes whether Otto needs a session id to stop looping. Set it on a test connector first. Full diagnosis in `OTTO_MCP_DIAGNOSIS.md`.

### `@SLED-MCP` must be in every Otto turn
Otto drops tool registration between turns. The `@` mention re-registers it. This is an Otto behavior, not fixable in the Lambda.

---

## End-to-End Validation Status (2026-07-07)

**Validated and working:**
- `score deal="Accenture/AZ AFIS (ERP) (2013)" focal=IBM` completed in **~140 s** at 4096 MB (peak 1857 MB)
- Produced: Accenture 52.4% on 8 base-rubric dimensions, all cells generated with evidence citations
- JSON + XLSX artifacts in `s3://sled-scoring-agent-bucket/outputs/Accenture_AZ_AFIS_ERP_2013/`
- Scheme fell back to `base_rubric` (no RFP text — the folder's only "RFP" was an xlsx, correctly skipped)
- `deal=Accenture` (whole company) fast-fails in seconds with a clear "input too large" error

**Not yet validated:**
- Extract path (FOIA scoresheet → real scores) — needs a folder with an actual scoresheet PDF
- True competitive scorecard (multiple vendors for the same RFP) — needs matched project set

---

## Pending / Next Steps

1. **Admin: disable Lambda async retry** — `aws lambda put-function-event-invoke-config --function-name sled-scoring-agent --maximum-retry-attempts 0`. Krish's IAM user is denied `PutFunctionEventInvokeConfig`. The code retry-noop prevents churn regardless, but the admin setting is cleaner.

2. **Enable the PowerPoint deck** — set env var `SCORING_PPTX_ENABLED=1` on the scoring Lambda
   (`aws lambda update-function-configuration`). The deck now renders **programmatically** (no
   template file needed) as a 7-slide IBM-branded competitive deck: Title → Overview → Final
   Scoring (Technical/Financial/Final rank) → Scoring Overview (RAG) → Detailed Scoring → Outcome
   Drivers (Why Won / Why IBM Lost) → Category Comparison. When enabled, the pipeline runs a `deck`
   step that adds up to 3 Bedrock calls (metadata extraction + two narrative generations), each
   deadline-aware and **fails soft** (a failure appends a warning and the deck still renders from
   scores). Off = JSON + XLSX only, no extra Bedrock spend. Price-to-win scenario slides are
   deferred (phase 2). Redeploy code first (see quick redeploy above).

3. **Matched project set for a true competitive scorecard** — upload a single procurement where you have RFP + all competing vendor proposals + official FOIA scoresheet to `s3://sled-scoring-agent-bucket/projects/<id>/rfp/`, `proposals/<Vendor>/`, `scoresheet/`. Use `score project=<id>` to score it. This is the only path to a multi-vendor ranked output.

4. **Otto connector stability** — investigate `MCP_STATEFUL_SESSIONS=1` on a test connector to see if returning a session id stops the init-loop. Share `OTTO_MCP_DIAGNOSIS.md` with the Otto team. Until fixed, users must include `@SLED-MCP` in every message and may need to re-add the connector if the loop starts.

5. **Bedrock model access** — if the user wants to upgrade to opus-4-8 or sonnet-5 later, an admin must enable those models in the Bedrock console (Model access → enable Anthropic models). Sonnet 4 is the confirmed-working model.

6. **Textract OCR path** — currently only reachable as a fallback when Bedrock OCR fails. For very large scanned filings, Textract async may be more reliable. Not yet exercised in production.
