"""Extract real scores from an official (FOIA) scoresheet.

When a project includes an official evaluation scoresheet (e.g. the WA CCWIS
composite scoresheet), we pull the *actual* scores rather than predicting them.
We target the composite/summary scores per vendor per dimension (not the raw
per-evaluator sheets), mapping them onto the scheme's canonical dimension ids.
Extracted cells are tagged ``provenance = EXTRACTED``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .models import Evidence, Provenance, ScoreCell, SchemeSpec

_SYSTEM = """You read official government proposal-evaluation scoresheets and \
report the final composite scores exactly as recorded. Use the summary/rollup \
scores (already averaged across evaluators) — do not re-derive from individual \
evaluator sheets. Report only numbers present in the document; use null when a \
value is not stated."""

_KEYWORDS = ["total", "composite", "final score", "weighted", "points", "rank", "summary",
             "average", "consensus", "overall"]


@dataclass
class ExtractionResult:
    by_vendor: Dict[str, Dict[str, ScoreCell]] = field(default_factory=dict)
    totals: Dict[str, Optional[float]] = field(default_factory=dict)

    def vendors(self) -> List[str]:
        return list(self.by_vendor.keys())


def _excerpt(text: str, vendor_hints: Optional[List[str]], max_chars: int = 26000) -> str:
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    lowered = text.lower()
    terms = list(_KEYWORDS) + [v.lower() for v in (vendor_hints or [])]
    windows: List[tuple] = [(0, min(len(text), 8000))]  # always include the front matter
    for term in terms:
        start = 0
        for _ in range(20):
            idx = lowered.find(term, start)
            if idx == -1:
                break
            windows.append((max(0, idx - 300), min(len(text), idx + 700)))
            start = idx + len(term)
    windows.sort()
    merged = [windows[0]]
    for s, e in windows[1:]:
        ls, le = merged[-1]
        if s <= le:
            merged[-1] = (ls, max(le, e))
        else:
            merged.append((s, e))
    out, total = [], 0
    for s, e in merged:
        chunk = text[s:e]
        if total + len(chunk) > max_chars:
            chunk = chunk[: max_chars - total]
        out.append(chunk)
        total += len(chunk)
        if total >= max_chars:
            break
    return "\n...\n".join(out)


def _build_prompt(excerpt: str, scheme: SchemeSpec, vendor_hints: Optional[List[str]]) -> str:
    dims = "\n".join(f"  - {d.id}: {d.name}" for d in scheme.dimensions)
    hint = ""
    if vendor_hints:
        hint = "\nExpected vendors (match names to these where possible): " + ", ".join(vendor_hints)
    return f"""Extract the final composite scores from this evaluation scoresheet.

Map each scored criterion onto one of these canonical dimension ids where it \
corresponds (use the id verbatim); include unmatched criteria with a new slug id:
{dims}
{hint}

Return JSON:
{{
  "vendors": [
    {{
      "name": "<vendor/bidder name as written>",
      "total": <final total score as a number, or null>,
      "scores": [
        {{"dimension_id": "<canonical id or slug>",
          "value": <numeric score on the sheet's scale, or null>,
          "native_value": "<verbatim score/label as written, or null>",
          "note": "<any evaluator note, or empty>"}}
      ]
    }}
  ]
}}

Report only values present in the document. Do not estimate.

SCORESHEET:
\"\"\"
{excerpt}
\"\"\"
"""


def extract_scores(
    scoresheet_text: str,
    scheme: SchemeSpec,
    doc_name: str = "scoresheet.pdf",
    vendor_hints: Optional[List[str]] = None,
    bedrock=None,
    warnings: Optional[List[str]] = None,
) -> ExtractionResult:
    warnings = warnings if warnings is not None else []
    result = ExtractionResult()

    excerpt = _excerpt(scoresheet_text, vendor_hints)
    if not excerpt.strip():
        return result

    if bedrock is None:
        from .bedrock import default_client

        bedrock = default_client()

    try:
        data = bedrock.converse_json(
            _build_prompt(excerpt, scheme, vendor_hints), system=_SYSTEM, max_tokens=8000
        )
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Scoresheet extraction failed ({exc}); scores will be generated instead.")
        return result

    for vrow in (data or {}).get("vendors", []):
        name = (vrow.get("name") or "").strip()
        if not name:
            continue
        cells: Dict[str, ScoreCell] = {}
        for s in vrow.get("scores", []):
            dim_id = s.get("dimension_id")
            if not dim_id:
                continue
            value = s.get("value")
            native = s.get("native_value")
            if value is None and native is None:
                continue
            cells[dim_id] = ScoreCell(
                vendor=name,
                dimension_id=dim_id,
                value=None if value is None else float(value),
                native_value=None if native is None else str(native),
                provenance=Provenance.EXTRACTED,
                rationale=(s.get("note") or "").strip(),
                evidence=[Evidence(doc=doc_name, quote="official scoresheet")],
            )
        if cells:
            result.by_vendor[name] = cells
            total = vrow.get("total")
            result.totals[name] = None if total is None else float(total)
    return result
