"""Generate predicted scores by evaluating each proposal against the scheme.

For every (vendor, dimension) not already covered by an extracted score, we:
  1. retrieve the most relevant proposal passages for that dimension
     (lightweight term-overlap retrieval over page-segmented text — no vector DB
     required; a Bedrock Knowledge Base can be swapped in later for scale),
  2. ask Claude to predict the score a government panel would assign under the
     solicitation's native scheme, with cited evidence, a rationale, and a
     confidence — through the competitive-intelligence lens.

Generated cells are tagged ``provenance = GENERATED`` (or ``GATE_FAIL`` when a
mandatory minimum requirement is judged unmet).
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from .ingest import DocumentText
from .models import Dimension, Evidence, Provenance, ScoreCell, ScoreMethod, SchemeSpec

_STOP = {
    "the", "and", "for", "with", "that", "this", "will", "are", "our", "from", "have",
    "has", "was", "were", "which", "their", "your", "you", "all", "any", "can", "not",
    "including", "include", "provide", "proposal", "vendor", "state", "system",
}

_SYSTEM = """You predict how a government source-selection panel would score a \
vendor's proposal under a specific solicitation's evaluation scheme. Score only \
from the evidence provided; if evidence is thin, lower the confidence rather than \
inventing strengths. Take a competitive-intelligence stance: judge how compelling \
this bid is relative to a strong field, and be candid about weaknesses."""


def _tokenize(text: str) -> List[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in _STOP]


def _flatten(docs: List[DocumentText]) -> List[Tuple[str, int, str]]:
    """[(doc_name, page_no, page_text), ...] across a vendor's proposal docs."""
    flat: List[Tuple[str, int, str]] = []
    for dt in docs:
        for i, page in enumerate(dt.pages):
            if page.strip():
                flat.append((dt.document.id, i + 1, page))
    return flat


def retrieve(
    pages: List[Tuple[str, int, str]], dim: Dimension, k: int = 2, cap: int = 900
) -> List[Tuple[str, int, str]]:
    """Top-k pages for a dimension by term overlap with its name/sub-criteria."""
    query = " ".join([dim.name, dim.description] + dim.sub_criteria)
    terms = set(_tokenize(query))
    if not terms or not pages:
        return [(d, p, t[:cap]) for d, p, t in pages[:k]]
    scored = []
    for doc, page_no, text in pages:
        toks = _tokenize(text)
        if not toks:
            continue
        overlap = sum(1 for t in toks if t in terms)
        scored.append((overlap / (len(toks) ** 0.5), doc, page_no, text))
    scored.sort(reverse=True)
    return [(doc, page_no, text[:cap]) for _, doc, page_no, text in scored[:k]]


def _evidence_block(
    scheme: SchemeSpec,
    pages: List[Tuple[str, int, str]],
    skip: set,
    k: int,
    total_cap: int = 16000,
) -> Tuple[str, Dict[str, List[Tuple[str, int]]]]:
    """Build the per-dimension evidence text + a map of dim -> [(doc,page)] cited."""
    blocks: List[str] = []
    cited: Dict[str, List[Tuple[str, int]]] = {}
    total = 0
    for dim in scheme.dimensions:
        if dim.id in skip:
            continue
        hits = retrieve(pages, dim, k=k)
        cited[dim.id] = [(doc, page_no) for doc, page_no, _ in hits]
        lines = [f"### DIMENSION {dim.id}: {dim.name}"]
        if dim.description:
            lines.append(f"(criterion: {dim.description.strip()})")
        for doc, page_no, text in hits:
            lines.append(f"[{doc} p.{page_no}] {text}")
        block = "\n".join(lines)
        if total + len(block) > total_cap:
            block = block[: max(0, total_cap - total)]
        blocks.append(block)
        total += len(block)
        if total >= total_cap:
            break
    return "\n\n".join(blocks), cited


def _scale_desc(scheme: SchemeSpec) -> str:
    s = scheme.scale
    anchors = "; ".join(f"{k}={v}" for k, v in sorted(s.anchors.items())) if s.anchors else ""
    base = f"Score each dimension on a {s.min:g}-{s.max:g} numeric scale (higher is better)."
    return base + (f" Anchors: {anchors}" if anchors else "")


def _build_prompt(vendor: str, scheme: SchemeSpec, focal_vendor: str, evidence: str) -> str:
    native = ""
    if scheme.method == ScoreMethod.BEST_VALUE_TRADEOFF:
        native = (
            "\nThis solicitation uses a best-value trade-off (no fixed points). Still "
            "assign a 1-5 comparison score per dimension, and put a short qualitative "
            "assessment (e.g. Strong/Adequate/Weak) in native_value."
        )
    focal_note = (
        f"\n(The focal vendor in this analysis is {focal_vendor}; be especially precise "
        f"about {vendor}'s competitiveness.)"
    )
    return f"""Predict the panel's scores for vendor "{vendor}" under this scheme.

Method: {scheme.method.value}. {_scale_desc(scheme)}{native}{focal_note}

For every dimension below, return a score based ONLY on the cited evidence.

Return JSON:
{{
  "scores": [
    {{"dimension_id": "<id>",
      "value": <number on the scale>,
      "native_value": "<points/label if the scheme is point-based or adjectival, else null>",
      "rationale": "<one or two sentences, competitive stance>",
      "confidence": <0-1>,
      "gate_pass": <true/false/null - only for mandatory minimum-requirement dimensions>,
      "evidence": [{{"page": <int>, "quote": "<short verbatim snippet>"}}]}}
  ]
}}

EVIDENCE BY DIMENSION:
{evidence}
"""


def score_vendor(
    vendor: str,
    docs: List[DocumentText],
    scheme: SchemeSpec,
    focal_vendor: str,
    skip_dims: Optional[set] = None,
    bedrock=None,
    k: int = 2,
    warnings: Optional[List[str]] = None,
) -> Dict[str, ScoreCell]:
    warnings = warnings if warnings is not None else []
    skip = set(skip_dims or set())
    pages = _flatten(docs)
    if not pages:
        warnings.append(f"No readable proposal text for {vendor}; dimensions left unscored.")
        return {}

    evidence, cited = _evidence_block(scheme, pages, skip, k=k)
    doc_name = docs[0].document.id if docs else "proposal.pdf"

    if bedrock is None:
        from .bedrock import default_client

        bedrock = default_client()

    try:
        data = bedrock.converse_json(
            _build_prompt(vendor, scheme, focal_vendor, evidence), system=_SYSTEM, max_tokens=8000
        )
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Scoring failed for {vendor} ({exc}).")
        return {}

    cells: Dict[str, ScoreCell] = {}
    for s in (data or {}).get("scores", []):
        dim_id = s.get("dimension_id")
        if not dim_id or dim_id in skip:
            continue
        dim = scheme.get_dimension(dim_id)
        gate_pass = s.get("gate_pass")
        provenance = Provenance.GENERATED
        value = s.get("value")
        if dim is not None and dim.mandatory_gate and gate_pass is False:
            provenance = Provenance.GATE_FAIL
            value = None
        evidence_objs = [
            Evidence(doc=doc_name, page=e.get("page"), quote=(e.get("quote") or "")[:400])
            for e in (s.get("evidence") or [])
        ]
        if not evidence_objs and cited.get(dim_id):
            evidence_objs = [Evidence(doc=d, page=p) for d, p in cited[dim_id]]
        cells[dim_id] = ScoreCell(
            vendor=vendor,
            dimension_id=dim_id,
            value=None if value is None else float(value),
            native_value=(str(s["native_value"]) if s.get("native_value") not in (None, "") else None),
            provenance=provenance,
            confidence=s.get("confidence"),
            rationale=(s.get("rationale") or "").strip(),
            evidence=evidence_objs,
        )
    return cells


def generate_scores(
    proposal_texts: Dict[str, List[DocumentText]],
    scheme: SchemeSpec,
    focal_vendor: str,
    extracted_by_vendor: Optional[Dict[str, Dict[str, ScoreCell]]] = None,
    bedrock=None,
    k: int = 2,
    warnings: Optional[List[str]] = None,
) -> Dict[str, Dict[str, ScoreCell]]:
    """Score every vendor, skipping dimensions already covered by extraction."""
    warnings = warnings if warnings is not None else []
    extracted_by_vendor = extracted_by_vendor or {}
    out: Dict[str, Dict[str, ScoreCell]] = {}
    for vendor, docs in proposal_texts.items():
        skip = set((extracted_by_vendor.get(vendor) or {}).keys())
        out[vendor] = score_vendor(
            vendor, docs, scheme, focal_vendor, skip_dims=skip, bedrock=bedrock, k=k, warnings=warnings
        )
    return out
