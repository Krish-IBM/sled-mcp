"""Competitor bid-strategy analysis pipeline.

Two model stages, mirroring how the scoring agent budgets work:

1. **Digest** (fast model, one call per procurement): extract per-dimension
   evidence from that procurement's documents.
2. **Synthesis** (strong model, one call): merge all digests into a
   cross-procurement strategy profile with implications for the focal vendor.

Both stages are deadline-aware — when the Lambda budget runs low the pipeline
stops digesting and synthesizes whatever it has, recording a warning instead of
dying mid-run.
"""

from __future__ import annotations

import json
import os
import time
from typing import Callable, Dict, List, Optional

from scoring_agent.bedrock import BedrockClient
from scoring_agent.ingest import Document, extract_text

from .corpus import list_competitor_docs, resolve_competitor
from .models import (
    DIMENSIONS,
    CompetitorAnalysis,
    DimensionFinding,
    EvidenceItem,
    ProcurementDigest,
)
from .render_docx import render_docx
from .render_json import render_json

# Budget knobs (env-tunable). Chars, not tokens: ~4 chars/token, so 150k chars
# keeps each digest call well inside the model's context window.
MAX_PROCUREMENTS = int(os.environ.get("CA_MAX_PROCUREMENTS", "25"))
MAX_DOCS_PER_PROCUREMENT = int(os.environ.get("CA_MAX_DOCS_PER_PROC", "6"))
MAX_CHARS_PER_DIGEST = int(os.environ.get("CA_MAX_CHARS_PER_DIGEST", "150000"))
MAX_CHARS_PER_DOC = int(os.environ.get("CA_MAX_CHARS_PER_DOC", "60000"))

_DEFAULT_MODEL = "us.anthropic.claude-sonnet-4-20250514-v1:0"


def default_client() -> BedrockClient:
    model = os.environ.get("CA_MODEL_ID", _DEFAULT_MODEL)
    fast = os.environ.get("CA_FAST_MODEL_ID", model)
    return BedrockClient(strong_model=model, fast_model=fast)


class AnalysisError(ValueError):
    """User-addressable failure (unknown competitor, empty folder, ...)."""


# --------------------------------------------------------------------------- #
# Stage 1: per-procurement digest
# --------------------------------------------------------------------------- #
def _dimension_prompt_block() -> str:
    return "\n".join(
        f'- "{key}" ({spec["title"]}): {spec["description"]}'
        for key, spec in DIMENSIONS.items()
    )


_DIGEST_SYSTEM = (
    "You are a competitive-intelligence analyst for government (SLED) procurements. "
    "You extract factual evidence about ONE vendor's bid strategy from FOIA documents "
    "(their proposals, pricing workbooks, evaluation scoresheets). Report only what the "
    "documents contain — never invent facts, figures, or outcomes. Quote specific "
    "numbers, names, and scores when present, and name the source document for each."
)


def _digest_procurement(
    bedrock: BedrockClient,
    competitor: str,
    procurement: str,
    texts: List[str],
    doc_names: List[str],
) -> ProcurementDigest:
    corpus = "\n\n".join(texts)[:MAX_CHARS_PER_DIGEST]
    prompt = (
        f"Vendor under analysis: {competitor}\n"
        f"Procurement folder: {procurement}\n"
        f"Documents provided: {', '.join(doc_names)}\n\n"
        "Extract this vendor's bid-strategy evidence from the document text below.\n"
        "Dimensions to cover:\n"
        f"{_dimension_prompt_block()}\n\n"
        "Return JSON: {\"client\": issuing agency/state or \"\", \"year\": procurement "
        "year or \"\", \"outcome\": \"won\"/\"lost\"/\"unknown\" with a short basis, "
        "\"dimension_notes\": {dimension key: 2-6 sentences of specific evidence, or "
        "\"\" if the documents are silent on it}}\n\n"
        "DOCUMENT TEXT:\n" + corpus
    )
    raw = bedrock.converse_json(prompt, system=_DIGEST_SYSTEM, fast=True, max_tokens=2000)
    if not isinstance(raw, dict):
        raise ValueError(f"digest for '{procurement}' was not a JSON object")
    # The model occasionally returns dimension_notes as a non-dict (list/string)
    # despite the prompt; coerce so .items() below can't blow up one procurement.
    notes = raw.get("dimension_notes")
    notes = notes if isinstance(notes, dict) else {}
    return ProcurementDigest(
        procurement=procurement,
        client=str(raw.get("client", "") or ""),
        year=str(raw.get("year", "") or ""),
        outcome=str(raw.get("outcome", "") or ""),
        dimension_notes={k: str(v) for k, v in notes.items() if k in DIMENSIONS and v},
        source_docs=doc_names,
    )


# --------------------------------------------------------------------------- #
# Stage 2: cross-procurement synthesis
# --------------------------------------------------------------------------- #
_SYNTH_SYSTEM = (
    "You are a senior competitive-intelligence strategist advising a focal vendor on "
    "how a specific competitor bids for government (SLED) work. You are given "
    "per-procurement evidence digests extracted from real FOIA documents. Synthesize "
    "PATTERNS across procurements — what this competitor repeatedly does — and ground "
    "every claim in the digests. Never invent evidence; where the digests are thin, "
    "say so plainly."
)


def _synthesize(
    bedrock: BedrockClient,
    competitor: str,
    focal: str,
    digests: List[ProcurementDigest],
) -> Dict:
    payload = json.dumps([d.to_dict() for d in digests], ensure_ascii=False)
    prompt = (
        f"Competitor: {competitor}\n"
        f"Focal vendor (the reader): {focal}\n"
        f"Evidence digests from {len(digests)} procurements:\n{payload}\n\n"
        "Produce the competitor's bid-strategy profile.\n"
        "Dimensions:\n"
        f"{_dimension_prompt_block()}\n\n"
        "Return JSON:\n"
        "{\n"
        '  "executive_summary": "5-8 sentence overview of how this competitor bids and '
        f'what it means for {focal}",\n'
        '  "dimensions": {\n'
        "    <dimension key>: {\n"
        '      "analysis": "cross-procurement narrative of their approach (1-2 paragraphs)",\n'
        '      "evidence": [{"procurement": "...", "detail": "specific fact, with source '
        'document when the digest names one"}],\n'
        f'      "ibm_implications": "what {focal} should do about it — how to counter, '
        'where they are vulnerable (3-5 sentences)"\n'
        "    }\n"
        "  }\n"
        "}\n"
        "Cover every dimension key. 3-6 evidence items per dimension when available."
    )
    raw = bedrock.converse_json(prompt, system=_SYNTH_SYSTEM, max_tokens=8000)
    if not isinstance(raw, dict) or not isinstance(raw.get("dimensions"), dict):
        raise ValueError("synthesis did not return the expected JSON shape")
    return raw


def _findings_from_synthesis(raw: Dict) -> List[DimensionFinding]:
    findings = []
    for key, spec in DIMENSIONS.items():
        # Guard against a truthy non-dict dimension value (e.g. the model returns
        # a bare string for a dimension). Without this the whole job would crash
        # AFTER every digest completed, instead of failing that dimension soft.
        dim = raw["dimensions"].get(key)
        dim = dim if isinstance(dim, dict) else {}
        evidence = [
            EvidenceItem(
                procurement=str(e.get("procurement", "") or ""),
                detail=str(e.get("detail", "") or ""),
            )
            for e in dim.get("evidence", []) or []
            if isinstance(e, dict)
        ]
        findings.append(
            DimensionFinding(
                key=key,
                title=spec["title"],
                analysis=str(dim.get("analysis", "") or "No evidence found in the corpus."),
                evidence=evidence,
                ibm_implications=str(dim.get("ibm_implications", "") or ""),
            )
        )
    return findings


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run_analysis(
    competitor_query: str,
    *,
    focal: str = "IBM",
    ci_bucket: str,
    out_bucket: str,
    out_prefix: str,
    procurement: Optional[str] = None,
    s3=None,
    bedrock: Optional[BedrockClient] = None,
    progress: Callable[[str, str], None] = lambda step, msg: None,
    deadline_epoch: Optional[float] = None,
) -> "AnalysisOutput":
    if s3 is None:
        import boto3

        s3 = boto3.client("s3")
    bedrock = bedrock or default_client()

    progress("resolve", f"Resolving competitor '{competitor_query}'")
    folder, candidates = resolve_competitor(competitor_query, ci_bucket, s3=s3)
    if folder is None:
        if candidates:
            raise AnalysisError(
                f"Ambiguous competitor '{competitor_query}'. Matches: "
                + "; ".join(candidates[:10])
                + '. Re-run with the exact name, e.g. analyze competitor="'
                + candidates[0] + '".'
            )
        raise AnalysisError(
            f"No competitor folder matches '{competitor_query}' in {ci_bucket}. "
            "Use 'competitors' to list available vendors."
        )

    progress("list", f"Listing FOIA documents for {folder}")
    groups, skipped = list_competitor_docs(
        ci_bucket, folder, procurement=procurement, s3=s3,
        max_docs_per_procurement=MAX_DOCS_PER_PROCUREMENT,
    )
    if not groups:
        scope = f" under procurement '{procurement}'" if procurement else ""
        raise AnalysisError(f"No readable documents found for '{folder}'{scope}.")

    # Biggest procurements first: more documents ≈ richer evidence, and if the
    # deadline cuts the run short we want the strongest digests already banked.
    ordered = sorted(groups.items(), key=lambda kv: -sum(d.size for d in kv[1]))
    analysis = CompetitorAnalysis(competitor=folder, focal=focal)
    if len(ordered) > MAX_PROCUREMENTS:
        analysis.warnings.append(
            f"{folder} has {len(ordered)} procurements; analyzed the {MAX_PROCUREMENTS} "
            "largest. Scope with procurement=\"<name>\" for the rest."
        )
        ordered = ordered[:MAX_PROCUREMENTS]
    if skipped:
        analysis.docs_skipped = skipped

    digests: List[ProcurementDigest] = []
    for i, (proc, docs) in enumerate(ordered, 1):
        if deadline_epoch is not None and time.time() > deadline_epoch:
            analysis.warnings.append(
                f"Time budget reached after {len(digests)}/{len(ordered)} procurements; "
                "the profile is based on those analyzed."
            )
            break
        progress("digest", f"[{i}/{len(ordered)}] Reading {proc} ({len(docs)} docs)")
        texts, names = [], []
        for doc in docs:
            try:
                dt = extract_text(doc, s3=s3, bedrock=bedrock, deadline_epoch=deadline_epoch)
            except Exception as exc:  # noqa: BLE001 — one bad file must not kill the run
                analysis.warnings.append(f"{proc}/{doc.id}: extraction failed ({exc})")
                continue
            if dt.char_count:
                texts.append(f"===== DOCUMENT: {doc.id} =====\n{dt.full_text[:MAX_CHARS_PER_DOC]}")
                names.append(doc.id)
            else:
                analysis.docs_skipped += 1
        if not texts:
            analysis.warnings.append(f"{proc}: no extractable text (scanned/unsupported files)")
            continue
        analysis.docs_analyzed += len(names)
        try:
            digests.append(_digest_procurement(bedrock, folder, proc, texts, names))
        except Exception as exc:  # noqa: BLE001
            analysis.warnings.append(f"{proc}: digest failed ({exc})")

    if not digests:
        raise AnalysisError(
            f"Could not extract analyzable evidence from any of {folder}'s documents. "
            + "; ".join(analysis.warnings[:3])
        )
    analysis.procurement_digests = digests

    progress("synthesize", f"Synthesizing strategy profile from {len(digests)} procurements")
    raw = _synthesize(bedrock, folder, focal, digests)
    analysis.executive_summary = str(raw.get("executive_summary", "") or "")
    analysis.dimensions = _findings_from_synthesis(raw)

    progress("render", "Rendering JSON + DOCX report")
    artifacts: Dict[str, str] = {}
    json_local = "/tmp/competitor_analysis.json"
    render_json(analysis, json_local)
    json_key = f"{out_prefix}competitor_analysis.json"
    s3.upload_file(json_local, out_bucket, json_key)
    artifacts["json"] = json_key

    try:
        docx_local = "/tmp/competitor_analysis.docx"
        render_docx(analysis, docx_local)
        docx_key = f"{out_prefix}competitor_analysis.docx"
        s3.upload_file(docx_local, out_bucket, docx_key)
        artifacts["docx"] = docx_key
    except Exception as exc:  # noqa: BLE001 — report rendering must not void the analysis
        analysis.warnings.append(f"DOCX report failed to render ({exc}); JSON is complete.")

    return AnalysisOutput(analysis=analysis, artifacts=artifacts)


class AnalysisOutput:
    def __init__(self, analysis: CompetitorAnalysis, artifacts: Dict[str, str]):
        self.analysis = analysis
        self.artifacts = artifacts
