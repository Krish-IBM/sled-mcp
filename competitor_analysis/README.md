# SLED Competitor-Analysis Agent

Backend Lambda (`sled-competitor-analysis-agent`) behind the MCP router's
`COMPETITOR_ANALYSIS_URL`. Profiles a competitor's bid strategy across the FOIA
corpus (`s3://competitive-intelligence-sled`, organized `<Vendor>/<Procurement>/…`)
on five dimensions — **solutioning, staffing, pricing, past performance &
references, win themes & differentiators** — each ending with implications for
the focal vendor (IBM by default). Outputs a chat summary plus downloadable
PowerPoint (PPTX), Word (DOCX), and JSON artifacts via presigned links.

## Commands (router forwards `{"query": "..."}`)

```
competitor_analysis: analyze competitor="Accenture" [procurement="AZ AFIS (ERP) (2013)"] [focal=IBM]
competitor_analysis: status <job_id>
competitor_analysis: result <job_id>
competitor_analysis: competitors          # list vendor folders in the corpus
```

The router's 29s timeout can't hold a full analysis, so `analyze` returns a
`job_id` immediately (async job model identical to the scoring agent: job
record in S3 + async self-invoke; auto-retry guard; stall detection).

## Pipeline

1. **Resolve** the competitor name to a vendor folder (fuzzy; ambiguity returns
   the candidate list). Working folders (`00_FOIA Analysis`, …) are excluded.
2. **Enumerate + prioritize** documents per procurement (pricing/proposal/
   technical/staffing/scoresheet filenames first), capped by
   `CA_MAX_DOCS_PER_PROC` (6) and `CA_MAX_PROCUREMENTS` (25, largest first).
3. **Digest** (fast model, 1 call/procurement): per-dimension evidence JSON.
   Text extraction reuses `scoring_agent.ingest` (pdfplumber + Bedrock-vision /
   Textract OCR for scanned docs), deadline-aware.
4. **Synthesize** (strong model, 1 call): cross-procurement strategy profile
   with evidence citations and focal-vendor implications.
5. **Render** JSON + DOCX (`python-docx`) + PPTX (`python-pptx`), all
   IBM-branded (IBM Plex Sans, IBM blue). The deck auto-paginates long
   narratives/evidence onto continuation slides so text never overlaps. Written
   to `s3://$CA_OUTPUT_BUCKET/competitor-analysis/outputs/<slug>/<job_id>/`.

Deadline handling mirrors the scoring agent: the pipeline stops digesting
`RENDER_MARGIN_SECONDS` (240) before the Lambda kill and synthesizes what it
has, recording a warning.

## Environment variables

| Var | Default | Meaning |
| --- | --- | --- |
| `CA_OUTPUT_BUCKET` | *(required)* | Job records + artifacts (use `sled-scoring-agent-bucket`) |
| `CI_BUCKET` | `competitive-intelligence-sled` | FOIA corpus |
| `CA_JOBS_PREFIX` / `CA_OUTPUT_PREFIX` | `competitor-analysis/{jobs,outputs}/` | S3 prefixes |
| `CA_MODEL_ID` / `CA_FAST_MODEL_ID` | Sonnet 4 inference profile | Bedrock models |
| `SELF_FUNCTION_NAME` | *(function name)* | async self-invoke target |
| `DEFAULT_FOCAL` | `IBM` | focal vendor |
| `CA_MAX_PROCUREMENTS` / `CA_MAX_DOCS_PER_PROC` / `CA_MAX_CHARS_PER_DIGEST` / `CA_MAX_CHARS_PER_DOC` / `CA_MAX_DOC_MB` | 25 / 6 / 150000 / 60000 / 150 | budget knobs |
| `PRESIGN_TTL`, `RENDER_MARGIN_SECONDS`, `JOB_STALE_GRACE_SECONDS` | 3600 / 240 / 30 | timing |

## Build & deploy

```
./build_competitor_package.sh        # build/competitor_analysis.zip (manylinux, no Docker)
./deploy_competitor_analysis.sh      # create/update Lambda + HTTP API, wire router env
```

Reuses the existing `sled-scoring-agent-role` (same pattern as
`sled-bid-analysis-agent`) — no IAM changes needed. The deploy script merges
`COMPETITOR_ANALYSIS_URL` into the router env (never overwrites it) and the
router code must contain the `competitor_analysis` registry entry
(see `lambda_handler.py` `AGENT_REGISTRY`).

Offline tests: `.venv/bin/python -m pytest tests/test_competitor_offline.py`.
