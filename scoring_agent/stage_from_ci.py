"""Stage deal documents from competitive-intelligence-sled into the scoring bucket.

When a user requests `score deal=<id>`, this module:
  1. Lists all files under deals/<id>/ in the CI bucket.
  2. Classifies each file as rfp / proposal / scoresheet using filename heuristics,
     falling back to Claude (fast model) for ambiguous files.
  3. Copies classified files server-side into the scoring bucket under the standard
     projects/<id>/{rfp,proposals/<Vendor>,scoresheet}/ layout that the pipeline
     already understands.
  4. Writes a manifest JSON so subsequent runs skip the copy entirely.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

_MANIFEST_KEY = "projects/{project_id}/.ci_manifest.json"

# Heuristics: patterns applied to the lowercased filename (no extension).
_RFP_RE = re.compile(
    r"\brfp\b|request.for.proposal|solicitation|invitation.to.bid|\bsow\b|\bifb\b"
)
_SHEET_RE = re.compile(
    r"scoresheet|score.sheet|scorecard|score.card|evaluation.result|evaluation.matrix"
    r"|foia|consensus.score|technical.score|final.score|evaluator"
)
_SKIP_RE = re.compile(
    r"\bamendment\b|\baddendum\b|\bq.and.a\b|\bq&a\b|\bfaq\b|\bexhibit\b"
    r"|\battachment\b|\bform\b|\.thumb|\bds_store\b|__macosx"
)

# Well-known vendor names (lowercase) for heuristic matching.
_KNOWN_VENDORS = [
    "ibm", "accenture", "deloitte", "ey", "kpmg", "pwc",
    "booz allen", "boozallen", "leidos", "saic", "cgi", "atos",
    "cognizant", "infosys", "tcs", "capgemini", "ntt", "unisys",
    "maximus", "perspecta", "dxc", "gartner", "mckinsey", "bah",
]


# --------------------------------------------------------------------------- #
# Data structures
# --------------------------------------------------------------------------- #
@dataclass
class ClassifiedFile:
    source_key: str
    category: str            # "rfp" | "proposal" | "scoresheet" | "unknown"
    vendor: Optional[str]    # non-None for category=="proposal"
    confidence: str          # "heuristic" | "claude" | "low"


@dataclass
class StagingManifest:
    project_id: str
    ci_bucket: str
    ci_deals_prefix: str
    staged_at: str
    staged_files: List[Dict[str, Any]] = field(default_factory=list)
    skipped_files: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_id": self.project_id,
            "ci_bucket": self.ci_bucket,
            "ci_deals_prefix": self.ci_deals_prefix,
            "staged_at": self.staged_at,
            "staged_files": self.staged_files,
            "skipped_files": self.skipped_files,
        }


# --------------------------------------------------------------------------- #
# Manifest helpers
# --------------------------------------------------------------------------- #
def get_manifest(scoring_bucket: str, project_id: str, s3=None) -> Optional[Dict[str, Any]]:
    """Return the parsed manifest dict if this deal has been staged, else None."""
    if s3 is None:
        import boto3
        s3 = boto3.client("s3")
    key = _MANIFEST_KEY.format(project_id=project_id)
    try:
        obj = s3.get_object(Bucket=scoring_bucket, Key=key)
        return json.loads(obj["Body"].read())
    except Exception:  # noqa: BLE001
        return None


def _write_manifest(manifest: StagingManifest, scoring_bucket: str, s3) -> None:
    key = _MANIFEST_KEY.format(project_id=manifest.project_id)
    s3.put_object(
        Bucket=scoring_bucket,
        Key=key,
        Body=json.dumps(manifest.to_dict(), indent=2).encode("utf-8"),
        ContentType="application/json",
    )


# --------------------------------------------------------------------------- #
# List CI deal files
# --------------------------------------------------------------------------- #
def list_ci_deal_files(
    ci_bucket: str,
    deal_id: str,
    deals_prefix: str = "deals/",
    s3=None,
) -> List[str]:
    """Return all non-folder S3 keys under deals/<deal_id>/ in the CI bucket."""
    if s3 is None:
        import boto3
        s3 = boto3.client("s3")
    parts = [p for p in [deals_prefix.strip("/"), deal_id.strip("/")] if p]
    prefix = "/".join(parts) + "/"
    paginator = s3.get_paginator("list_objects_v2")
    keys: List[str] = []
    for page in paginator.paginate(Bucket=ci_bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            k = obj["Key"]
            if not k.endswith("/"):
                keys.append(k)
    return keys


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def _heuristic_classify(key: str, deal_id: str, default_vendor: Optional[str] = None) -> ClassifiedFile:
    """Best-effort classification using filename patterns.

    ``default_vendor`` is set when scoping to a single company sub-procurement
    folder: every non-RFP/non-scoresheet file is that company's proposal, so we
    attribute it to ``default_vendor`` instead of leaving it "unknown" (which
    otherwise explodes one company's docs into dozens of pseudo-vendors).
    """
    basename = os.path.basename(key)
    stem = re.sub(r"\.[^.]+$", "", basename).lower()
    stem_clean = re.sub(r"[_\-\.]+", " ", stem)

    if _SKIP_RE.search(stem_clean):
        return ClassifiedFile(source_key=key, category="unknown", vendor=None, confidence="heuristic")
    if _RFP_RE.search(stem_clean):
        return ClassifiedFile(source_key=key, category="rfp", vendor=None, confidence="heuristic")
    if _SHEET_RE.search(stem_clean):
        return ClassifiedFile(source_key=key, category="scoresheet", vendor=None, confidence="heuristic")

    # Within a scoped company folder we KNOW the vendor, so the company hint wins
    # over _infer_vendor's flaky filename guessing (which otherwise invents a new
    # "vendor" per file, e.g. "Some Technical" from "...Response.pdf").
    vendor = default_vendor or _infer_vendor(key, deal_id)
    if vendor:
        return ClassifiedFile(source_key=key, category="proposal", vendor=vendor, confidence="heuristic")

    return ClassifiedFile(source_key=key, category="unknown", vendor=None, confidence="heuristic")


def _infer_vendor(key: str, deal_id: str) -> Optional[str]:
    """Extract a vendor name from path segments or known vendor list in filename."""
    parts = [p for p in key.replace("\\", "/").split("/") if p and p.lower() != deal_id.lower()]
    # drop the deals prefix and deal_id itself (first 2 segments)
    parts = parts[2:] if len(parts) > 2 else parts

    # Check each segment against known vendors
    for part in parts:
        p = part.lower()
        for v in _KNOWN_VENDORS:
            if v in p:
                # Return the original casing of the path segment as vendor name
                return part.split(".")[0]  # strip extension if present

    # Look for the word "proposal" next to a probable company name in the stem
    stem = re.sub(r"\.[^.]+$", "", os.path.basename(key)).lower()
    m = re.search(r"([a-z][a-z &]+?)\s*(?:proposal|bid|response|submission)", stem)
    if m:
        candidate = m.group(1).strip()
        if len(candidate) >= 2 and candidate not in ("final", "technical", "cost", "the"):
            return candidate.title()

    return None


def classify_ci_files(
    keys: List[str],
    deal_id: str,
    bedrock=None,
    default_vendor: Optional[str] = None,
) -> List[ClassifiedFile]:
    """Classify S3 keys — heuristics first, Claude for remaining unknowns.

    When ``default_vendor`` is given (single-company sub-procurement scope),
    heuristics attribute unmatched files to that vendor, so we rarely need Claude
    and never fragment into pseudo-vendors.
    """
    results: List[ClassifiedFile] = []
    ambiguous: List[str] = []

    for key in keys:
        cf = _heuristic_classify(key, deal_id, default_vendor=default_vendor)
        if cf.category == "unknown":
            ambiguous.append(key)
        else:
            results.append(cf)

    if ambiguous and bedrock is not None:
        claude_results = _claude_classify(ambiguous, deal_id, bedrock)
        results.extend(claude_results)
    else:
        # No bedrock or nothing ambiguous — mark all unknowns as low-confidence unknown
        for key in ambiguous:
            results.append(ClassifiedFile(source_key=key, category="unknown", vendor=None, confidence="low"))

    return results


def _claude_classify(keys: List[str], deal_id: str, bedrock) -> List[ClassifiedFile]:
    """Ask Claude to classify a batch of ambiguous file paths."""
    # Send in chunks of 50 to avoid very long prompts
    all_results: List[ClassifiedFile] = []
    for i in range(0, len(keys), 50):
        chunk = keys[i:i + 50]
        all_results.extend(_claude_classify_chunk(chunk, deal_id, bedrock))
    return all_results


def _claude_classify_chunk(keys: List[str], deal_id: str, bedrock) -> List[ClassifiedFile]:
    key_lines = "\n".join(keys)
    prompt = (
        f'Classify these S3 file paths from government procurement deal "{deal_id}".\n'
        'For each path return its category and vendor (if applicable).\n\n'
        'Categories:\n'
        '- "rfp": the solicitation / request for proposals\n'
        '- "scoresheet": official evaluation results (FOIA scoresheet, evaluation matrix)\n'
        '- "proposal": a vendor\'s bid response — infer vendor name from path/filename\n'
        '- "unknown": none of the above (forms, press releases, unrelated attachments)\n\n'
        f'File paths:\n{key_lines}\n\n'
        'Return a JSON array: [{"key": "<original key>", "category": "...", "vendor": null or "<Vendor Name>"}]'
    )
    try:
        raw = bedrock.converse_json(prompt, fast=True, max_tokens=2000)
        if not isinstance(raw, list):
            raise ValueError("expected JSON array")
        key_map = {k: k for k in keys}
        out: List[ClassifiedFile] = []
        classified_keys = set()
        for item in raw:
            k = item.get("key", "")
            if k not in key_map:
                continue
            classified_keys.add(k)
            cat = item.get("category", "unknown")
            vendor = item.get("vendor") or None
            if cat == "proposal" and not vendor:
                vendor = _infer_vendor(k, deal_id) or f"Vendor-{len(out) + 1}"
            out.append(ClassifiedFile(source_key=k, category=cat, vendor=vendor, confidence="claude"))
        # Any keys Claude didn't return — mark unknown
        for k in keys:
            if k not in classified_keys:
                out.append(ClassifiedFile(source_key=k, category="unknown", vendor=None, confidence="low"))
        return out
    except Exception:  # noqa: BLE001
        return [ClassifiedFile(source_key=k, category="unknown", vendor=None, confidence="low") for k in keys]


# --------------------------------------------------------------------------- #
# Staging (server-side S3 copy)
# --------------------------------------------------------------------------- #
def stage_deal_to_scoring_bucket(
    classified: List[ClassifiedFile],
    ci_bucket: str,
    scoring_bucket: str,
    project_id: str,
    deals_prefix: str = "deals/",
    s3=None,
) -> StagingManifest:
    """Copy classified CI files into the scoring bucket under projects/<project_id>/."""
    if s3 is None:
        import boto3
        s3 = boto3.client("s3")

    manifest = StagingManifest(
        project_id=project_id,
        ci_bucket=ci_bucket,
        ci_deals_prefix=deals_prefix,
        staged_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )

    vendor_counters: Dict[str, int] = {}

    for cf in classified:
        basename = os.path.basename(cf.source_key)
        if cf.category == "rfp":
            dest_key = f"projects/{project_id}/rfp/{basename}"
        elif cf.category == "scoresheet":
            dest_key = f"projects/{project_id}/scoresheet/{basename}"
        elif cf.category == "proposal":
            vendor = cf.vendor or "Unknown"
            # Deduplicate vendor name clashes (e.g. two files with vendor="IBM")
            dest_key = f"projects/{project_id}/proposals/{vendor}/{basename}"
        else:
            manifest.skipped_files.append(cf.source_key)
            continue

        s3.copy_object(
            CopySource={"Bucket": ci_bucket, "Key": cf.source_key},
            Bucket=scoring_bucket,
            Key=dest_key,
        )
        manifest.staged_files.append({
            "ci_key": cf.source_key,
            "scoring_key": dest_key,
            "category": cf.category,
            "vendor": cf.vendor,
            "confidence": cf.confidence,
        })

    _write_manifest(manifest, scoring_bucket, s3)
    return manifest
