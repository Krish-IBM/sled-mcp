"""Lambda entry point for the SLED competitor-analysis agent.

The MCP router forwards ``{"query": "..."}`` and reads back ``response`` with a
hard 29s timeout, so a full analysis (dozens of FOIA documents + multiple
Bedrock calls) cannot complete synchronously. Same async job model as the
scoring agent:

    competitor_analysis: analyze competitor=<name> [procurement="..."] [focal=IBM]
        -> start a job, return a job_id
    competitor_analysis: status <job_id>   -> progress
    competitor_analysis: result <job_id>   -> summary + presigned download links
    competitor_analysis: competitors       -> list vendor folders in the corpus

An "analyze" request writes a job record to S3 and self-invokes this Lambda
asynchronously (InvocationType=Event). The async pass runs the pipeline,
updating the job record at every step, and stores JSON/DOCX in S3.
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

from .pipeline import AnalysisError, run_analysis

# ── config (env) ──────────────────────────────────────────────────────────── #
OUTPUT_BUCKET = os.environ.get("CA_OUTPUT_BUCKET", "")
JOBS_PREFIX = os.environ.get("CA_JOBS_PREFIX", "competitor-analysis/jobs/").rstrip("/") + "/"
OUTPUT_PREFIX = os.environ.get("CA_OUTPUT_PREFIX", "competitor-analysis/outputs/").rstrip("/") + "/"
PRESIGN_TTL = int(os.environ.get("PRESIGN_TTL", "3600"))
DEFAULT_FOCAL = os.environ.get("DEFAULT_FOCAL", "IBM")
CI_BUCKET = os.environ.get("CI_BUCKET", "competitive-intelligence-sled")

# Reserve time at the end of the Lambda budget so synthesis + render + upload
# finish before the hard kill, plus a grace window before a still-"running"
# job is declared dead.
RENDER_MARGIN_SECONDS = int(os.environ.get("RENDER_MARGIN_SECONDS", "240"))
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
    slug = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")
    return slug[:120] or "competitor"


def _parse_iso(ts: str) -> Optional[float]:
    try:
        # timestamps are UTC ("...Z"); timegm treats the tuple as UTC.
        return calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except Exception:  # noqa: BLE001
        return None


def _is_stalled(job: Dict[str, Any]) -> bool:
    """True if a job claims to be running but its worker's Lambda budget is spent."""
    if job.get("status") not in ("running", "started"):
        return False
    now = time.time()
    deadline = job.get("worker_deadline_epoch")
    if deadline is not None:
        try:
            return now > float(deadline) + JOB_STALE_GRACE_SECONDS
        except (TypeError, ValueError):
            pass
    started = _parse_iso(job.get("updated_at", "")) if job.get("updated_at") else None
    if started is None:
        return False
    return now > started + DEFAULT_LAMBDA_BUDGET_SECONDS + JOB_STALE_GRACE_SECONDS


_STALLED_MSG = (
    "appears to have stalled — its worker exceeded the Lambda time budget "
    "(usually too many/large documents). Scope the run, e.g. "
    'analyze competitor="<name>" procurement="<one procurement>".'
)


# ── query parsing ─────────────────────────────────────────────────────────── #
def _parse(query: str):
    q = (query or "").strip()
    low = q.lower()
    if low.startswith("analyze") or low.startswith("analyse"):
        rest = q.split(None, 1)[1].strip() if len(q.split(None, 1)) > 1 else ""
        cm = re.search(r'competitor\s*=\s*("[^"]+"|[^\s]+)', rest, re.I)
        pm = re.search(r'procurement\s*=\s*("[^"]+"|[^\s]+)', rest, re.I)
        fm = re.search(r'focal\s*=\s*("[^"]+"|[^\s]+)', rest, re.I)
        focal = fm.group(1).strip('"') if fm else DEFAULT_FOCAL
        if cm:
            return "analyze", {
                "competitor": cm.group(1).strip('"'),
                "procurement": pm.group(1).strip('"') if pm else None,
                "focal": focal,
            }
        # bare form: "analyze Accenture"
        if rest and "=" not in rest:
            return "analyze", {"competitor": rest.strip('"'), "procurement": None, "focal": focal}
        return "help", {}
    m = re.match(r"(status|result)\s+(\S+)", low)
    if m:
        return m.group(1), {"job_id": m.group(2)}
    if low.startswith("competitors") or low.startswith("list"):
        return "competitors", {}
    return "help", {}


_HELP = (
    "SLED competitor-analysis agent. Analyzes a competitor's bid strategy across all "
    "their FOIA documents (solutioning, staffing, pricing, past performance, win themes) "
    "with implications for IBM. Commands:\n"
    '  analyze competitor="<name>" [procurement="<name>"] [focal=IBM]\n'
    "      — start an analysis (competitor name matches a vendor folder in the corpus)\n"
    "  status <job_id>      — check progress\n"
    "  result <job_id>      — summary + JSON/DOCX download links\n"
    "  competitors          — list vendors available in the FOIA corpus\n"
    "Large competitors take several minutes; scope with procurement=\"...\" to go faster."
)


# ── request handlers ──────────────────────────────────────────────────────── #
def _list_competitors() -> Dict[str, Any]:
    from .corpus import list_vendor_folders

    try:
        folders = list_vendor_folders(CI_BUCKET)
    except Exception as exc:  # noqa: BLE001
        return _respond(f"Could not list the FOIA corpus: {exc}", 500)
    return _respond(
        f"{len(folders)} vendors in the FOIA corpus:\n" + "\n".join(f"  - {f}" for f in folders)
    )


def _start_job(args: Dict[str, Any], context) -> Dict[str, Any]:
    competitor = (args.get("competitor") or "").strip()
    if not competitor:
        return _respond('Missing competitor. Use: analyze competitor="<name>"')
    if not OUTPUT_BUCKET:
        return _respond("Server misconfigured: CA_OUTPUT_BUCKET is not set.", 500)

    job_id = uuid.uuid4().hex[:12]
    state = {
        "job_id": job_id,
        "competitor": competitor,
        "procurement": args.get("procurement"),
        "focal": args.get("focal", DEFAULT_FOCAL),
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
    scope = f" (procurement: {args['procurement']})" if args.get("procurement") else ""
    return _respond(
        f"Started competitor analysis job {job_id} for '{competitor}'{scope} "
        f"(focal: {state['focal']}).\n"
        f"Check progress: competitor_analysis: status {job_id}\n"
        f"Get results:    competitor_analysis: result {job_id}"
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

    lines: List[str] = [
        f"Competitor bid-strategy analysis: {job.get('resolved_competitor', job['competitor'])} "
        f"(focal: {job.get('focal')})",
        "",
        job.get("executive_summary", ""),
        "",
        f"Based on {job.get('procurements_analyzed', 0)} procurements / "
        f"{job.get('docs_analyzed', 0)} FOIA documents.",
    ]
    if job.get("dimension_headlines"):
        lines.append("")
        for head in job["dimension_headlines"]:
            lines.append(f"  - {head}")
    if job.get("warnings"):
        lines += ["", "Notes:"] + [f"  - {w}" for w in job["warnings"][:5]]

    s3 = _s3()
    outputs = job.get("outputs") or {}
    labels = {"docx": "WORD REPORT (DOCX)", "json": "FULL ANALYSIS (JSON)"}
    ordered = [k for k in ("docx", "json") if k in outputs]
    ordered.extend(k for k in outputs if k not in labels)
    if ordered:
        lines += ["", "Downloads (links expire; Word report is listed first):"]
        for kind in ordered:
            lines.append(f"  {labels.get(kind, kind.upper())}: {_presign(outputs[kind], s3)}")
    return _respond("\n".join(lines))


# ── async worker ──────────────────────────────────────────────────────────── #
def _run_job(state: Dict[str, Any], context) -> None:
    s3 = _s3()
    job_id = state["job_id"]

    # Retry guard: a timed-out async worker is auto-retried by Lambda with the
    # SAME payload. Read the live record and refuse to re-run a job that already
    # had an attempt or already resolved.
    live = _read_job(job_id, s3) or state
    if live.get("status") in ("error", "done"):
        return
    prior_attempts = int(live.get("attempts", 0) or 0)
    if prior_attempts >= 1:
        live.update(
            status="error", step="error", message="Analysis failed",
            error=("Worker exceeded its time budget on the first attempt and was auto-retried; "
                   "not re-running. Scope the analysis with procurement=\"<name>\" or a smaller "
                   "competitor."),
        )
        _write_job(live, s3)
        return
    state["attempts"] = prior_attempts + 1

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

    def progress(step: str, message: str) -> None:
        state.update(status="running", step=step, message=message)
        _write_job(state, s3)

    state.update(status="running", step="queued", message="Worker started")
    _write_job(state, s3)

    try:
        slug = _slugify(state["competitor"])
        out = run_analysis(
            state["competitor"],
            focal=state.get("focal", DEFAULT_FOCAL),
            ci_bucket=CI_BUCKET,
            out_bucket=OUTPUT_BUCKET,
            out_prefix=f"{OUTPUT_PREFIX}{slug}/{job_id}/",
            procurement=state.get("procurement"),
            s3=s3,
            progress=progress,
            deadline_epoch=pipeline_deadline,
        )
        analysis = out.analysis
        state.update(
            status="done",
            step="done",
            message="Analysis complete",
            resolved_competitor=analysis.competitor,
            executive_summary=analysis.executive_summary,
            dimension_headlines=[
                f"{d.title}: {(d.analysis or '').split('. ')[0][:220]}"
                for d in analysis.dimensions
            ],
            procurements_analyzed=len(analysis.procurement_digests),
            docs_analyzed=analysis.docs_analyzed,
            outputs=out.artifacts,
            warnings=analysis.warnings,
        )
        _write_job(state, s3)
    except AnalysisError as exc:
        state.update(status="error", step="error", message="Analysis failed", error=str(exc))
        _write_job(state, s3)
    except Exception as exc:  # noqa: BLE001
        state.update(status="error", step="error", message="Analysis failed",
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
    if verb == "analyze":
        return _start_job(args, context)
    if verb == "status":
        return _status(args["job_id"])
    if verb == "result":
        return _result(args["job_id"])
    if verb == "competitors":
        return _list_competitors()
    return _respond(_HELP)
