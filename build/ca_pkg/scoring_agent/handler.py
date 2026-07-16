"""Lambda entry point for the SLED scoring agent (behind SCORING_AGENT_URL).

The MCP router forwards ``{"query": "..."}`` and reads back ``response`` with a
hard 29s timeout, so a full scoring run cannot complete synchronously. This
handler therefore implements an async job model:

    scoring: score project=<id> [focal=IBM]   -> start a job, return a job_id
    scoring: status <job_id>                   -> progress
    scoring: result <job_id>                   -> summary + presigned download links

A "score" request writes a job record to S3 and self-invokes this same Lambda
asynchronously (InvocationType=Event). The async pass runs the pipeline, updating
the job record at every step, and stores JSON/XLSX/PPTX in S3.
"""

from __future__ import annotations

import calendar
import json
import os
import re
import time
import traceback
import uuid
from typing import Any, Dict, List, Optional

from .pipeline import run_scoring

# ── config (env) ──────────────────────────────────────────────────────────── #
BUCKET = os.environ.get("SCORING_BUCKET", "")
OUTPUT_BUCKET = os.environ.get("SCORING_OUTPUT_BUCKET", "") or BUCKET
JOBS_PREFIX = os.environ.get("JOBS_PREFIX", "jobs/").rstrip("/") + "/"
OUTPUT_PREFIX = os.environ.get("OUTPUT_PREFIX", "outputs/").rstrip("/") + "/"
PRESIGN_TTL = int(os.environ.get("PRESIGN_TTL", "3600"))
DEFAULT_FOCAL = os.environ.get("DEFAULT_FOCAL", "IBM")
TEMPLATE_S3 = os.environ.get("SCORING_PPTX_TEMPLATE_S3", "")
TEMPLATE_PATH_ENV = os.environ.get("SCORING_PPTX_TEMPLATE_PATH", "")
CI_BUCKET = os.environ.get("CI_BUCKET", "")
_ci_prefix_raw = os.environ.get("CI_DEALS_PREFIX", "").strip("/")
CI_DEALS_PREFIX = (_ci_prefix_raw + "/") if _ci_prefix_raw else ""

# Reserve time at the end of the Lambda budget so in-flight OCR can drain and
# render + upload can finish before the hard kill, plus a grace window before we
# declare a still-"running" job dead.
RENDER_MARGIN_SECONDS = int(os.environ.get("RENDER_MARGIN_SECONDS", "300"))
JOB_STALE_GRACE_SECONDS = int(os.environ.get("JOB_STALE_GRACE_SECONDS", "30"))
DEFAULT_LAMBDA_BUDGET_SECONDS = int(os.environ.get("DEFAULT_LAMBDA_BUDGET_SECONDS", "900"))


# ── small helpers ─────────────────────────────────────────────────────────── #
def _s3():
    import boto3

    return boto3.client("s3")


def _respond(text: str, status: int = 200) -> Dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"response": text}),
    }


def _job_key(job_id: str) -> str:
    return f"{JOBS_PREFIX}{job_id}.json"


def _write_job(state: Dict[str, Any], s3=None) -> None:
    s3 = s3 or _s3()
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    s3.put_object(
        Bucket=OUTPUT_BUCKET,
        Key=_job_key(state["job_id"]),
        Body=json.dumps(state).encode("utf-8"),
        ContentType="application/json",
    )


def _read_job(job_id: str, s3=None) -> Optional[Dict[str, Any]]:
    s3 = s3 or _s3()
    try:
        obj = s3.get_object(Bucket=OUTPUT_BUCKET, Key=_job_key(job_id))
        return json.loads(obj["Body"].read())
    except Exception:  # noqa: BLE001
        return None


def _presign(key: str, s3=None) -> str:
    s3 = s3 or _s3()
    return s3.generate_presigned_url(
        "get_object", Params={"Bucket": OUTPUT_BUCKET, "Key": key}, ExpiresIn=PRESIGN_TTL
    )


def _slugify(value: str) -> str:
    """Turn a CI deal path (may contain '/' and spaces) into a safe staging id."""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")
    return slug[:120] or "deal"


def _parse_iso(ts: str) -> Optional[float]:
    try:
        # timestamps are UTC ("...Z"); timegm treats the tuple as UTC (mktime would
        # wrongly apply the local offset).
        return calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except Exception:  # noqa: BLE001
        return None


def _is_stalled(job: Dict[str, Any]) -> bool:
    """True if a job claims to be running but its worker's Lambda budget is spent.

    Lambda hard-kills the async worker at its timeout, after which the job record
    is frozen forever at its last step. We detect that so status/result report a
    failure instead of a perpetual "running".
    """
    if job.get("status") not in ("running", "started"):
        return False
    now = time.time()
    deadline = job.get("worker_deadline_epoch")
    if deadline is not None:
        try:
            return now > float(deadline) + JOB_STALE_GRACE_SECONDS
        except (TypeError, ValueError):
            pass
    # older jobs without a recorded deadline: fall back to updated_at staleness
    started = _parse_iso(job.get("updated_at", "")) if job.get("updated_at") else None
    if started is None:
        return False
    return now > started + DEFAULT_LAMBDA_BUDGET_SECONDS + JOB_STALE_GRACE_SECONDS


_STALLED_MSG = (
    "appears to have stalled — its worker exceeded the Lambda time budget "
    "(usually too many/large documents). Scope to one procurement, e.g. "
    "scoring: score deal=\"Company/Subfolder\", or upload a matched project set."
)


# ── query parsing ─────────────────────────────────────────────────────────── #
def _parse(query: str):
    q = (query or "").strip()
    low = q.lower()
    if low.startswith("score"):
        rest = q[5:].strip()
        # Quote a value to include spaces/slashes, e.g. deal="Accenture/AZ AFIS (2013)".
        dm = re.search(r'deal\s*=\s*("[^"]+"|[^\s]+)', rest, re.I)
        pm = re.search(r'project\s*=\s*("[^"]+"|[^\s]+)', rest, re.I)
        fm = re.search(r'focal\s*=\s*("[^"]+"|[^\s]+)', rest, re.I)
        focal = fm.group(1).strip('"') if fm else DEFAULT_FOCAL
        if dm:
            return "score", {"deal": dm.group(1).strip('"').strip("/"), "focal": focal}
        if pm:
            return "score", {"project": pm.group(1).strip('"'), "focal": focal}
        project = rest.split()[0] if rest.split() else ""
        return "score", {"project": project, "focal": focal}
    m = re.match(r"(status|result)\s+(\S+)", low)
    if m:
        return m.group(1), {"job_id": m.group(2)}
    return "help", {}


_HELP = (
    "SLED scoring agent. Commands:\n"
    "  score deal=<id> [focal=IBM]              — score a deal folder from competitive-intelligence-sled\n"
    "  score deal=\"<Company>/<Procurement>\"     — score ONE procurement inside a company folder\n"
    "  score project=<id> [focal=IBM]           — score a project already in the scoring bucket\n"
    "  status <job_id>                          — check progress\n"
    "  result <job_id>                          — get ranking + download links\n"
    "The CI bucket is organized by company, and a company folder holds many unrelated\n"
    "procurements. Scoping to one procurement (quote the path) keeps the run within budget."
)


# ── request handlers ──────────────────────────────────────────────────────── #
def _start_job(args: Dict[str, str], context) -> Dict[str, Any]:
    deal = args.get("deal")
    project = args.get("project")
    if not deal and not project:
        return _respond("Missing id. Use: score deal=<id> or score project=<id> [focal=IBM]")
    if deal and not CI_BUCKET:
        return _respond("Server misconfigured: CI_BUCKET is not set.", 500)
    if not deal and not BUCKET:
        return _respond("Server misconfigured: SCORING_BUCKET is not set.", 500)

    # For deal=, the CI path may contain '/' and spaces (a company sub-procurement);
    # the staging/project id must be a clean single-segment slug.
    ci_path = deal or None
    project_id = _slugify(deal) if deal else project
    job_id = uuid.uuid4().hex[:12]
    state = {
        "job_id": job_id,
        "project_id": project_id,
        "ci_path": ci_path,
        "focal": args.get("focal", DEFAULT_FOCAL),
        "source": "ci" if deal else "scoring",
        "status": "started",
        "step": "queued",
        "message": "Job queued",
        "outputs": {},
        "warnings": [],
    }
    _write_job(state)

    # self-invoke asynchronously
    import boto3

    fn = os.environ.get("SELF_FUNCTION_NAME") or getattr(context, "function_name", "")
    boto3.client("lambda").invoke(
        FunctionName=fn,
        InvocationType="Event",
        Payload=json.dumps({"__job__": state}).encode("utf-8"),
    )
    source_label = f"CI deal '{deal}'" if deal else f"project '{project}'"
    return _respond(
        f"Started scoring job {job_id} for {source_label} (focal: {state['focal']}).\n"
        f"Check progress: scoring: status {job_id}\n"
        f"Get results:    scoring: result {job_id}"
    )


def _status(job_id: str) -> Dict[str, Any]:
    job = _read_job(job_id)
    if not job:
        return _respond(f"No job '{job_id}' found.")
    if _is_stalled(job):
        return _respond(f"Job {job_id} {_STALLED_MSG}")
    line = f"Job {job_id} [{job['status']}] — {job.get('step')}: {job.get('message')}"
    if job.get("status") == "error" and job.get("error"):
        line += f"\nError: {job['error']}"
    return _respond(line)


def _result(job_id: str) -> Dict[str, Any]:
    job = _read_job(job_id)
    if not job:
        return _respond(f"No job '{job_id}' found.")
    if _is_stalled(job):
        return _respond(f"Job {job_id} {_STALLED_MSG}")
    status = job.get("status")
    if status == "error":
        return _respond(f"Job {job_id} failed: {job.get('error', 'unknown error')}")
    if status != "done":
        return _respond(f"Job {job_id} is still running ({job.get('step')}: {job.get('message')}).")

    lines: List[str] = [f"Scorecard for project '{job['project_id']}' (focal: {job.get('focal')}):", ""]
    for row in job.get("ranking", []):
        tag = "DQ" if row.get("disqualified") else f"#{row.get('rank')}"
        extra = f" — {row['native_total']:.0f} pts" if row.get("native_total") is not None else ""
        lines.append(f"  {tag}  {row['vendor']}  ({row.get('normalized_pct', 0):.0f}%{extra})")
    if job.get("ci_summary"):
        lines += ["", "Competitive intelligence: " + job["ci_summary"]]
    if job.get("warnings"):
        lines += ["", "Notes:"] + [f"  - {w}" for w in job["warnings"][:5]]

    s3 = _s3()
    outputs = job.get("outputs") or {}
    labels = {
        "pptx": "POWERPOINT DECK (PPTX)",
        "xlsx": "EXCEL SCORECARD (XLSX)",
        "json": "JSON SCORECARD (JSON)",
    }
    ordered_kinds = [kind for kind in ("pptx", "xlsx", "json") if kind in outputs]
    ordered_kinds.extend(kind for kind in outputs if kind not in labels)
    lines += ["", "Downloads (links expire; PowerPoint deck is listed first):"]
    for kind in ordered_kinds:
        lines.append(f"  {labels.get(kind, kind.upper())}: {_presign(outputs[kind], s3)}")
    return _respond("\n".join(lines))


# ── async worker ──────────────────────────────────────────────────────────── #
def _resolve_template() -> Optional[str]:
    if TEMPLATE_PATH_ENV and os.path.exists(TEMPLATE_PATH_ENV):
        return TEMPLATE_PATH_ENV
    if TEMPLATE_S3:
        try:
            dest = "/tmp/scorecard_template.pptx"
            _s3().download_file(OUTPUT_BUCKET, TEMPLATE_S3, dest)
            return dest
        except Exception:  # noqa: BLE001
            return None
    packaged = os.path.join(os.path.dirname(__file__), "assets", "template.pptx")
    return packaged if os.path.exists(packaged) else None


def _run_job(state: Dict[str, Any], context) -> None:
    s3 = _s3()
    job_id = state["job_id"]

    # Retry guard: a timed-out async worker is auto-retried by Lambda with the
    # SAME payload (and we can't disable that via EventInvokeConfig here). Read
    # the live record and refuse to re-run a job that already had an attempt or
    # already resolved — otherwise a stuck job churns 3×900s.
    live = _read_job(job_id, s3) or state
    if live.get("status") in ("error", "done"):
        return
    prior_attempts = int(live.get("attempts", 0) or 0)
    if prior_attempts >= 1:
        live.update(
            status="error", step="error", message="Scoring failed",
            error=("Worker exceeded its time budget on the first attempt and was auto-retried; "
                   "not re-running. The documents need too much OCR for one run — scope to a "
                   "smaller sub-procurement or pre-convert scanned PDFs to text."),
        )
        _write_job(live, s3)
        return
    state["attempts"] = prior_attempts + 1

    # Wall-clock budget: record when this worker's Lambda will be killed so
    # (a) the pipeline can stop gracefully with margin for render/upload, and
    # (b) status/result can detect a worker that died mid-run (see _is_stalled).
    remaining_ms = DEFAULT_LAMBDA_BUDGET_SECONDS * 1000
    getter = getattr(context, "get_remaining_time_in_millis", None)
    if callable(getter):
        try:
            remaining_ms = getter()
        except Exception:  # noqa: BLE001
            pass
    worker_deadline = time.time() + remaining_ms / 1000.0
    state["worker_deadline_epoch"] = worker_deadline
    pipeline_deadline = worker_deadline - RENDER_MARGIN_SECONDS

    # deal= paths may carry '/' + spaces; the CI source uses the raw path, while
    # the staging/output bucket uses the clean slug stored as project_id.
    project_id = state["project_id"]
    ci_path = state.get("ci_path") or project_id

    def progress(step: str, message: str) -> None:
        state.update(status="running", step=step, message=message)
        _write_job(state, s3)

    # persist attempts + deadline up front so an auto-retry sees this attempt
    state.update(status="running", step="queued", message="Worker started")
    _write_job(state, s3)

    try:
        # -- CI staging gate: copy deal docs to scoring bucket on first run --
        if state.get("source") == "ci" and CI_BUCKET:
            from .ingest import check_staging_manifest
            manifest = check_staging_manifest(BUCKET, project_id, s3=s3)
            if manifest is None:
                progress("stage", f"Staging '{ci_path}' from {CI_BUCKET}")
                from .bedrock import default_client
                from .stage_from_ci import (
                    classify_ci_files,
                    list_ci_deal_files,
                    stage_deal_to_scoring_bucket,
                )
                keys = list_ci_deal_files(CI_BUCKET, ci_path, CI_DEALS_PREFIX, s3=s3)
                if not keys:
                    raise ValueError(
                        f"No files found under {CI_DEALS_PREFIX}{ci_path}/ in {CI_BUCKET}"
                    )
                # Within one company sub-procurement folder every doc is that
                # company's — hint the classifier so proposals don't fragment
                # into dozens of pseudo-vendors.
                company_hint = ci_path.split("/")[0] if "/" in ci_path else None
                classified = classify_ci_files(
                    keys, ci_path, bedrock=default_client(), default_vendor=company_hint
                )
                mf = stage_deal_to_scoring_bucket(
                    classified, CI_BUCKET, BUCKET, project_id, CI_DEALS_PREFIX, s3=s3
                )
                state["warnings"] = state.get("warnings", []) + [
                    f"Staged {len(mf.staged_files)} files from {CI_BUCKET}. "
                    f"{len(mf.skipped_files)} unclassified files skipped."
                ]
            else:
                progress("stage", f"Using cached staging for '{project_id}' (staged {manifest.get('staged_at', 'previously')})")

        out = run_scoring(
            project_id,
            focal_vendor=state.get("focal", DEFAULT_FOCAL),
            bucket=BUCKET,
            out_bucket=OUTPUT_BUCKET,
            out_prefix=f"{OUTPUT_PREFIX}{project_id}/{job_id}/",
            template_path=_resolve_template(),
            s3=s3,
            progress=progress,
            deadline_epoch=pipeline_deadline,
        )
        result = out.result
        ranking = [
            {
                "vendor": v.vendor,
                "rank": v.rank,
                "disqualified": v.disqualified,
                "normalized_pct": v.normalized_total_pct,
                "native_total": v.native_total,
            }
            for v in result.ranked_vendors()
        ]
        state.update(
            status="done",
            step="done",
            message="Scoring complete",
            outputs=out.artifacts,
            ranking=ranking,
            ci_summary=(result.ci.summary if result.ci else ""),
            warnings=result.warnings,
        )
        _write_job(state, s3)
    except Exception as exc:  # noqa: BLE001
        state.update(status="error", step="error", message="Scoring failed",
                     error=f"{exc}", traceback=traceback.format_exc()[-2000:])
        _write_job(state, s3)


# ── dispatch ──────────────────────────────────────────────────────────────── #
def lambda_handler(event, context):
    # async self-invocation
    if isinstance(event, dict) and event.get("__job__"):
        _run_job(event["__job__"], context)
        return {"ok": True}

    # HTTP (API Gateway / Function URL proxy)
    body = event.get("body") if isinstance(event, dict) else None
    if isinstance(body, str):
        try:
            body = json.loads(body or "{}")
        except json.JSONDecodeError:
            body = {}
    elif body is None:
        body = event if isinstance(event, dict) else {}
    query = (body or {}).get("query", "")

    verb, args = _parse(query)
    if verb == "score":
        return _start_job(args, context)
    if verb == "status":
        return _status(args["job_id"])
    if verb == "result":
        return _result(args["job_id"])
    return _respond(_HELP)
