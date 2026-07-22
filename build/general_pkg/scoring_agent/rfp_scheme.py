"""Parse a solicitation's *native* evaluation scheme into a SchemeSpec.

We mirror each RFP's real methodology (weighted-points like WA CCWIS, best-value
trade-off like NC, adjectival, or pass/fail + points) rather than forcing a
single scale. The base rubric supplies canonical dimension ids/names so the
per-RFP scheme stays comparable across projects (the "hybrid" model); Claude
maps the RFP's criteria onto those ids and adds/renames/reweights as needed.

If no evaluation section is found or parsing fails, we fall back to the base
rubric and record a warning.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from .models import ScoreMethod, SchemeSpec

# Keywords that mark the evaluation/scoring portion of an RFP.
_EVAL_KEYWORDS = [
    "evaluation criteria", "evaluation method", "evaluation process", "will be evaluated",
    "best value", "source selection", "scoring", "points will be", "weight", "weighting",
    "award will be", "technical proposal", "adjectival", "past performance",
    "minimum requirements", "responsive", "trade-off", "tradeoff", "point allocation",
]

_SYSTEM = """You are an expert government-procurement analyst. You extract the \
EXACT evaluation scheme a solicitation uses to score competing proposals, so it \
can be reproduced faithfully. Do not invent criteria or weights that are not \
stated or clearly implied; when the solicitation is qualitative (e.g. best-value \
trade-off with a priority order rather than points), represent it as such."""


def find_evaluation_excerpt(text: str, max_chars: int = 16000) -> str:
    """Extract the passages most likely to describe the evaluation methodology.

    Scans for evaluation keywords and gathers windows of surrounding text,
    keeping the result under ``max_chars`` to bound the prompt.
    """
    if not text:
        return ""
    lowered = text.lower()
    windows: List[Tuple[int, int]] = []
    for kw in _EVAL_KEYWORDS:
        start = 0
        while True:
            idx = lowered.find(kw, start)
            if idx == -1:
                break
            windows.append((max(0, idx - 600), min(len(text), idx + 1400)))
            start = idx + len(kw)
    if not windows:
        return text[:max_chars]

    # merge overlapping windows
    windows.sort()
    merged: List[Tuple[int, int]] = [windows[0]]
    for s, e in windows[1:]:
        ls, le = merged[-1]
        if s <= le:
            merged[-1] = (ls, max(le, e))
        else:
            merged.append((s, e))

    parts: List[str] = []
    total = 0
    for s, e in merged:
        chunk = text[s:e]
        if total + len(chunk) > max_chars:
            chunk = chunk[: max_chars - total]
        parts.append(chunk)
        total += len(chunk)
        if total >= max_chars:
            break
    return "\n...\n".join(parts)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "dimension"


def _build_prompt(excerpt: str, base: SchemeSpec) -> str:
    canonical = "\n".join(f"  - {d.id}: {d.name}" for d in base.dimensions)
    return f"""From the solicitation excerpt below, extract the native evaluation scheme.

Prefer these CANONICAL dimension ids where an RFP criterion corresponds to one \
(reuse the id so projects stay comparable); add new ids only for criteria that \
don't fit any:
{canonical}

Return JSON with this exact shape:
{{
  "method": "weighted_points | best_value_tradeoff | adjectival | pass_fail_plus_points",
  "scale": {{"type": "numeric|adjectival", "min": <num>, "max": <num>,
             "higher_is_better": true,
             "anchors": {{"1": "label/desc", "5": "label/desc"}}}},
  "cost_handling": "dimension | tradeoff",
  "cost_formula": "<string or null - any stated price-scoring formula>",
  "total_max_points": <number or null - total available points if point-based>,
  "gates": ["<minimum requirement whose failure disqualifies a vendor>", ...],
  "dimensions": [
    {{"id": "<canonical id or new slug>", "name": "<criterion name>",
      "description": "<short>",
      "weight": <fraction 0-1 or null>,
      "priority": <int rank, 1=most important, or null>,
      "max_points": <number or null>,
      "mandatory_gate": <bool>,
      "cost_related": <bool>,
      "sub_criteria": ["..."]}}
  ]
}}

Rules:
- If the RFP allocates points/percentages, set method "weighted_points" and fill \
"weight" (fraction) and/or "max_points" per dimension and "total_max_points".
- If it uses a best-value trade-off / ranked importance without fixed points, set \
method "best_value_tradeoff" and fill "priority" (1 = most important) instead of weights.
- If it uses adjectival ratings (Excellent/Good/…), set method "adjectival" and put \
the ratings in "scale.anchors".
- Mark any pass/fail minimum requirement in "gates" and set "mandatory_gate": true on \
the corresponding dimension if one exists.
- Use only what the text supports.

SOLICITATION EXCERPT:
\"\"\"
{excerpt}
\"\"\"
"""


def parse_scheme(
    rfp_text: str,
    project_id: str,
    base: SchemeSpec,
    bedrock=None,
    warnings: Optional[List[str]] = None,
) -> SchemeSpec:
    """Parse the RFP into a native SchemeSpec, falling back to ``base``."""
    warnings = warnings if warnings is not None else []

    excerpt = find_evaluation_excerpt(rfp_text)
    if not excerpt.strip():
        warnings.append("No RFP text available; using base rubric as the scheme.")
        base.project_id = project_id
        base.source = "base_rubric"
        return base

    if bedrock is None:
        from .bedrock import default_client

        bedrock = default_client()

    try:
        data = bedrock.converse_json(_build_prompt(excerpt, base), system=_SYSTEM, max_tokens=4000)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"RFP scheme parse failed ({exc}); using base rubric.")
        base.project_id = project_id
        base.source = "base_rubric"
        return base

    data = dict(data or {})
    data["project_id"] = project_id
    data["source"] = "parsed_from_rfp"

    # normalize/patch dimensions
    seen = set()
    for dim in data.get("dimensions") or []:
        did = dim.get("id") or _slug(dim.get("name", "dimension"))
        while did in seen:
            did += "_x"
        dim["id"] = did
        seen.add(did)
    if not data.get("dimensions"):
        warnings.append("RFP parse returned no dimensions; using base rubric dimensions.")
        data["dimensions"] = [d.to_dict() for d in base.dimensions]

    try:
        scheme = SchemeSpec.from_dict(data)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"RFP scheme JSON invalid ({exc}); using base rubric.")
        base.project_id = project_id
        base.source = "base_rubric"
        return base

    # sanity: weighted_points with no weights/points at all -> borrow base weights
    if scheme.method == ScoreMethod.WEIGHTED_POINTS and all(
        d.weight is None and d.max_points is None for d in scheme.dimensions
    ):
        base_w = {d.id: d.weight for d in base.dimensions}
        for d in scheme.dimensions:
            d.weight = base_w.get(d.id)
        if all(d.weight is None for d in scheme.dimensions):
            warnings.append("Weighted-points scheme lacked weights; using equal weights.")
    return scheme
