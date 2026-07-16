"""FOIA corpus access: resolve a competitor folder and enumerate its documents.

The competitive-intelligence bucket is organized ``<Vendor>/<Procurement>/...``,
with some small vendors keeping files directly under ``<Vendor>/``. A handful of
top-level folders are not vendors at all (working folders like ``00_FOIA
Analysis``) — they are excluded from resolution so a fuzzy name never lands on
one.
"""

from __future__ import annotations

import os
import re
from typing import Dict, List, Optional, Tuple

from scoring_agent.ingest import Document

# Top-level folders that are working areas, not vendors. Compared lowercased.
_NON_VENDOR_FOLDERS = (
    "00_foia analysis",
    "01_new 2026",
    "04 deal qualification",
)

# Files whose names suggest high analysis value get processed first. Ordered by
# priority; first match wins.
_PRIORITY_PATTERNS = (
    r"pricing|price|cost|rate|budget|financial",
    r"proposal|response|offer|bafo|bid",
    r"technical|solution|approach|sow|scope",
    r"staff|team|resume|personnel|key",
    r"score|evaluation|eval",
)

# Extensions extract_text can actually read (PDF + plain text). Everything else
# (Office binaries, archives, images) would be skipped downstream, so don't let
# it consume a document slot.
_READABLE_EXT = {".pdf", ".txt", ".md", ".csv", ".json", ".log"}

# Skip pathological objects rather than downloading them.
_MAX_DOC_BYTES = int(os.environ.get("CA_MAX_DOC_MB", "150")) * 1024 * 1024


def _s3_client(s3=None):
    if s3 is not None:
        return s3
    import boto3

    return boto3.client("s3")


def list_vendor_folders(bucket: str, s3=None) -> List[str]:
    """Top-level folder names in the CI bucket, excluding working folders."""
    s3 = _s3_client(s3)
    folders: List[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Delimiter="/"):
        for pre in page.get("CommonPrefixes", []):
            name = pre["Prefix"].rstrip("/")
            if name.lower() not in _NON_VENDOR_FOLDERS:
                folders.append(name)
    return sorted(folders)


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def resolve_competitor(
    name: str, bucket: str, s3=None
) -> Tuple[Optional[str], List[str]]:
    """Map a user-supplied competitor name to a vendor folder.

    Returns ``(folder, candidates)``: an exact/unique match fills ``folder``;
    otherwise ``candidates`` carries the near matches so the caller can ask the
    user to disambiguate.
    """
    folders = list_vendor_folders(bucket, s3=s3)
    want = _normalize(name)
    if not want:
        return None, []

    by_norm = {_normalize(f): f for f in folders}
    if want in by_norm:
        return by_norm[want], []

    # substring match either way ("accenture" -> "Accenture"; "hp" -> "HP (& EDS)")
    hits = [
        f for norm, f in by_norm.items()
        if want in norm or norm in want
        or want in norm.split() or set(want.split()) <= set(norm.split())
    ]
    if len(hits) == 1:
        return hits[0], []
    return None, sorted(hits)


def _priority(key: str) -> int:
    base = os.path.basename(key).lower()
    for i, pat in enumerate(_PRIORITY_PATTERNS):
        if re.search(pat, base):
            return i
    return len(_PRIORITY_PATTERNS)


def list_competitor_docs(
    bucket: str,
    vendor_folder: str,
    procurement: Optional[str] = None,
    s3=None,
    max_docs_per_procurement: int = 6,
) -> Tuple[Dict[str, List[Document]], int]:
    """Enumerate the competitor's documents, grouped by procurement.

    Loose files directly under the vendor folder group under ``"(general)"``.
    Within each procurement, documents are priority-ordered and capped at
    ``max_docs_per_procurement`` so one document-heavy procurement can't starve
    the rest of the run. Returns ``(groups, skipped_count)`` where skipped
    counts unreadable/oversized/over-cap files.
    """
    s3 = _s3_client(s3)
    prefix = f"{vendor_folder}/"
    if procurement:
        prefix += f"{procurement.strip('/')}/"

    groups: Dict[str, List[Document]] = {}
    skipped = 0
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            rel = key[len(f"{vendor_folder}/"):]
            parts = rel.split("/")
            group = parts[0] if len(parts) > 1 else "(general)"
            ext = os.path.splitext(key)[1].lower()
            if ext not in _READABLE_EXT or obj.get("Size", 0) > _MAX_DOC_BYTES:
                skipped += 1
                continue
            groups.setdefault(group, []).append(
                Document(
                    category="proposal",
                    filename=os.path.basename(key),
                    vendor=vendor_folder,
                    s3_key=key,
                    bucket=bucket,
                    size=obj.get("Size", 0),
                )
            )

    for group, docs in groups.items():
        docs.sort(key=lambda d: (_priority(d.s3_key or ""), -d.size))
        if len(docs) > max_docs_per_procurement:
            skipped += len(docs) - max_docs_per_procurement
            groups[group] = docs[:max_docs_per_procurement]
    return groups, skipped
