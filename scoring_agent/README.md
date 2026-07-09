# SLED Bid-Scoring Agent

Backend service behind the MCP router's `SCORING_AGENT_URL`. Produces a
side-by-side scorecard (evaluation dimensions × vendors + notes) for competing
bids on a government procurement, through a **competitive-intelligence lens**
(how does IBM stack up, and why).

It **extracts** real scores from an official FOIA scoresheet where one exists and
**generates** predicted scores (Claude on Bedrock) where one doesn't, **mirroring
each RFP's native scheme** (weighted-points, best-value trade-off, adjectival,
pass/fail + points). Every score is tagged `extracted` vs `generated`; generated
scores cite proposal evidence.

## Pipeline

```
load project docs (S3 or local dir)
  → ingest / OCR            ingest.py      (pdfplumber; Textract/Bedrock for scanned)
  → parse native scheme     rfp_scheme.py  (RFP eval section → SchemeSpec)
  → extract real scores     extract_scores.py  (official scoresheet, if present)
  → generate scores         score_generate.py  (per-dimension evidence + Claude)
  → merge + aggregate + CI  pipeline.py / aggregate.py
  → render                  render_json.py / render_excel.py / render_pptx.py
```

`handler.py` wraps this in an **async job model** (the MCP router has a hard 29s
timeout): a `score` request starts a job and self-invokes the Lambda; the async
pass runs the pipeline, updating job state in S3 and writing outputs.

## MCP commands (via the `sled_agent` tool)

```
scoring: score project=<id> [focal=IBM]   → returns a job_id
scoring: status <job_id>                   → progress
scoring: result <job_id>                   → ranking + presigned JSON/XLSX/PPTX links
```

## S3 / directory layout for a project

```
projects/<id>/rfp/...                  the solicitation / RFP
projects/<id>/proposals/<Vendor>/...   one subfolder per competing vendor (incl. IBM)
projects/<id>/scoresheet/...           official FOIA scoresheet (optional)
outputs/<id>/<job_id>/scorecard.{json,xlsx,pptx}
jobs/<job_id>.json                     async job state
```

## Environment variables

| Var | Purpose |
| --- | --- |
| `SCORING_BUCKET` | S3 bucket holding `projects/` inputs |
| `SCORING_OUTPUT_BUCKET` | outputs bucket (defaults to `SCORING_BUCKET`) |
| `SCORING_MODEL_ID` | **strong** Claude inference-profile id (scheme parse + scoring) |
| `SCORING_FAST_MODEL_ID` | **fast** Claude id (OCR/ingest) |
| `BEDROCK_REGION` | defaults to `AWS_REGION` |
| `DEFAULT_FOCAL` | focal vendor, default `IBM` |
| `SELF_FUNCTION_NAME` | this Lambda's name (for async self-invoke) |
| `SCORING_PPTX_TEMPLATE_S3` | optional S3 key of the `.pptx` template |
| `PRESIGN_TTL` | download-link lifetime seconds (default 3600) |

## Build & deploy (Docker-free)

```bash
# from the repo root
./build_scoring_package.sh --template "/path/to/Scorecard Template.pptx"

SCORING_BUCKET=my-sled-scoring \
SCORING_MODEL_ID=<strong-claude-inference-profile> \
SCORING_FAST_MODEL_ID=<fast-claude-inference-profile> \
PPTX_TEMPLATE_LOCAL="/path/to/Scorecard Template.pptx" \
./deploy_scoring.sh
```

`deploy_scoring.sh` creates the S3 bucket, IAM role (S3 + Bedrock + Textract +
self-invoke + logs), the `sled-scoring-agent` Lambda, and an HTTP API, then sets
`SCORING_AGENT_URL` on `sled-mcp-server`.

## Local testing (no AWS)

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r scoring_agent/requirements-scoring.txt
./.venv/bin/python tests/test_offline.py        # models, aggregate, CI, renderers vs real template
./.venv/bin/python tests/test_pipeline_mock.py  # full pipeline with a mock Bedrock client
```

## Design notes / knobs

* **Scale mirroring**: the authoritative score is the RFP's native scheme; a
  normalized 0–100 % comparison score is always computed so vendors stay
  comparable across differing schemes (`SchemeSpec.effective_weights`).
* **Retrieval**: per-dimension term-overlap over page-segmented text (no vector
  DB). Swap in a Bedrock Knowledge Base for very large corpora.
* **OCR**: scanned PDFs → Textract (async, S3) with a Bedrock-document-block
  fallback. Detection is automatic (`ingest.is_scanned`).
* **Scoresheet-only competitors**: vendors that appear on the official scoresheet
  but have no proposal in the folder are still included (extracted scores only).

## Still needed from the user (see plan)

1. **One matched project set** in the S3 layout above (RFP + all proposals +
   official scoresheet) for a true end-to-end run — current sample files are from
   different projects.
2. The SLED team's **own base rubric** (else `rubric/base_rubric.yaml` is used).
3. **AWS specifics**: account/region, bucket name, and the Claude model IDs
   enabled in Bedrock.
