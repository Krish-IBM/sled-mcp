"""End-to-end scoring pipeline.

    load project docs -> ingest/OCR -> parse native scheme -> extract real scores
    -> generate predicted scores -> merge -> aggregate + CI -> render JSON/XLSX/PPTX

Works against S3 (production) or a local directory (testing). A ``progress``
callback lets the async Lambda job report step-by-step status.
"""

from __future__ import annotations

import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .aggregate import aggregate
from .extract_scores import ExtractionResult, extract_scores
from .ingest import (
    Document,
    DocumentText,
    ProjectDocs,
    extract_text,
    load_project_from_dir,
    load_project_from_s3,
)
from .models import ScorecardResult, ScoreCell, VendorResult
from .render_excel import render_excel
from .render_json import render_json
from .rfp_scheme import parse_scheme
from .rubric import load_base_rubric
from .score_generate import generate_scores

Progress = Callable[[str, str], None]


def _noop(step: str, message: str) -> None:  # default progress sink
    pass


@dataclass
class ScoringOutput:
    result: ScorecardResult
    artifacts: Dict[str, str] = field(default_factory=dict)   # kind -> path or s3 key


def _norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def _match_vendor(name: str, candidates: List[str]) -> Optional[str]:
    """Match a scoresheet vendor name to a proposal-folder vendor name."""
    n = _norm_name(name)
    for c in candidates:
        cn = _norm_name(c)
        if n == cn or (len(n) >= 3 and (n in cn or cn in n)):
            return c
    return None


def _texts(docs: List[Document], *, s3, textract, bedrock, deadline_epoch=None) -> List[DocumentText]:
    if len(docs) <= 1:
        return [extract_text(d, s3=s3, textract=textract, bedrock=bedrock,
                             deadline_epoch=deadline_epoch) for d in docs]
    from concurrent.futures import ThreadPoolExecutor, as_completed
    results: dict = {}
    with ThreadPoolExecutor(max_workers=min(len(docs), 5)) as pool:
        futures = {pool.submit(extract_text, d, s3=s3, textract=textract, bedrock=bedrock,
                               deadline_epoch=deadline_epoch): i
                   for i, d in enumerate(docs)}
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                results[idx] = fut.result()
            except Exception:  # noqa: BLE001
                results[idx] = DocumentText(document=docs[idx], pages=[], method="none")
    return [results[i] for i in range(len(docs))]


def _concat(dts: List[DocumentText]) -> str:
    return "\n\n".join(dt.full_text for dt in dts if dt.pages)


def run_scoring(
    project_id: str,
    focal_vendor: str = "IBM",
    *,
    bucket: Optional[str] = None,
    local_dir: Optional[str] = None,
    s3=None,
    textract=None,
    bedrock=None,
    out_dir: Optional[str] = None,
    out_bucket: Optional[str] = None,
    out_prefix: Optional[str] = None,
    template_path: Optional[str] = None,
    retrieval_k: int = 2,
    progress: Progress = _noop,
    deadline_epoch: Optional[float] = None,
) -> ScoringOutput:
    warnings: List[str] = []

    max_docs = int(os.environ.get("SCORING_MAX_DOCS", "60"))
    max_total_mb = int(os.environ.get("SCORING_MAX_TOTAL_MB", "400"))

    def _budget(step: str) -> None:
        """Fail fast (clean job error) instead of grinding into the Lambda kill."""
        if deadline_epoch is not None and time.time() > deadline_epoch:
            raise TimeoutError(
                f"Scoring time budget exhausted before '{step}'. The input is likely too "
                f"large; scope to a single procurement (deal=\"Company/Subfolder\")."
            )

    # 1. load
    progress("load", "Loading project documents")
    if local_dir:
        proj: ProjectDocs = load_project_from_dir(local_dir, project_id)
    elif bucket:
        proj = load_project_from_s3(bucket, project_id, s3=s3)
    else:
        raise ValueError("run_scoring requires either bucket= or local_dir=")
    if not proj.proposal_docs and not proj.scoresheet_docs:
        raise ValueError(f"No proposals or scoresheet found for project '{project_id}'")

    # 1b. input-size guardrail — the #1 cause of the async worker blowing its
    # 900s Lambda budget was `deal=<company>` staging a whole-company corpus.
    docs = proj.all_documents()
    total_bytes = sum(getattr(d, "size", 0) or 0 for d in docs)
    if len(docs) > max_docs or total_bytes > max_total_mb * 1024 * 1024:
        raise ValueError(
            f"Input too large for one scoring run: {len(docs)} documents / "
            f"{total_bytes / 1e6:.0f} MB (limits: {max_docs} docs, {max_total_mb} MB). "
            f"This usually means the deal points at a whole-company corpus rather than a "
            f"single procurement. Scope it (e.g. deal=\"Company/Subfolder\") or upload a "
            f"matched project set to projects/<id>/."
        )

    # 2. ingest text (RFP, scoresheet, proposals)
    progress("ingest", "Extracting text (OCR where needed)")
    rfp_dts = _texts(proj.rfp_docs, s3=s3, textract=textract, bedrock=bedrock, deadline_epoch=deadline_epoch)
    rfp_text = _concat(rfp_dts)
    scoresheet_dts = _texts(proj.scoresheet_docs, s3=s3, textract=textract, bedrock=bedrock, deadline_epoch=deadline_epoch)
    scoresheet_text = _concat(scoresheet_dts)
    proposal_texts: Dict[str, List[DocumentText]] = {
        vendor: _texts(docs, s3=s3, textract=textract, bedrock=bedrock, deadline_epoch=deadline_epoch)
        for vendor, docs in proj.proposal_docs.items()
    }

    # 2b. surface anything ingestion couldn't read
    all_dts = list(rfp_dts) + list(scoresheet_dts) + [dt for dts in proposal_texts.values() for dt in dts]
    skipped = [dt.document.id for dt in all_dts if dt.method == "skipped"]
    skipped_deadline = [dt.document.id for dt in all_dts if dt.method == "skipped-deadline"]
    if skipped:
        preview = ", ".join(skipped[:5]) + ("…" if len(skipped) > 5 else "")
        warnings.append(f"Skipped {len(skipped)} unsupported file(s) (archive/Office/image not yet ingested): {preview}")
    if skipped_deadline:
        warnings.append(f"{len(skipped_deadline)} scanned document(s) were not OCR'd — time budget reached during ingest.")

    # 3. native scheme
    _budget("scheme")
    progress("scheme", "Parsing the RFP's native evaluation scheme")
    scheme = parse_scheme(rfp_text, project_id, load_base_rubric(project_id), bedrock=bedrock, warnings=warnings)

    # 4. extract real scores (if a scoresheet exists)
    extraction = ExtractionResult()
    if scoresheet_text.strip():
        _budget("extract")
        progress("extract", "Extracting official scores from the scoresheet")
        sheet_name = proj.scoresheet_docs[0].id if proj.scoresheet_docs else "scoresheet.pdf"
        extraction = extract_scores(
            scoresheet_text, scheme, doc_name=sheet_name,
            vendor_hints=proj.vendors(), bedrock=bedrock, warnings=warnings,
        )

    # 5. generate predicted scores for proposals
    _budget("generate")
    progress("generate", "Scoring proposals against the scheme")
    generated = generate_scores(
        proposal_texts, scheme, focal_vendor, extraction.by_vendor,
        bedrock=bedrock, k=retrieval_k, warnings=warnings,
    )

    # 6. merge extracted + generated into vendor results
    progress("merge", "Merging extracted and generated scores")
    result = _merge(project_id, focal_vendor, scheme, proj, generated, extraction, warnings)

    # 7. aggregate + CI
    progress("aggregate", "Computing totals, ranking, and CI insights")
    aggregate(result)
    # preserve official totals for fully-extracted vendors
    for v in result.vendors:
        off = extraction.totals.get(v.vendor)
        if off is not None:
            v.native_total = off

    # 8. deck content — only when a PowerPoint will be produced, so JSON/XLSX-only
    # runs don't pay for the extra (Bedrock) narrative + metadata calls.
    pptx_enabled = _pptx_enabled(template_path)
    deck_content = None
    if pptx_enabled:
        _budget("deck")
        progress("deck", "Building deck narrative + metadata")
        from .deck_content import build_deck_content

        deck_content = build_deck_content(
            result, rfp_text=rfp_text, proposal_texts=proposal_texts, proj=proj,
            bedrock=bedrock, deadline_epoch=deadline_epoch, warnings=warnings,
        )

    # 9. render
    progress("render", "Rendering JSON / Excel / PowerPoint")
    output = _render(result, out_dir=out_dir, out_bucket=out_bucket, out_prefix=out_prefix,
                     template_path=template_path, pptx_enabled=pptx_enabled,
                     deck_content=deck_content, s3=s3)
    progress("done", "Scoring complete")
    return output


def _pptx_enabled(template_path: Optional[str]) -> bool:
    """Deck output is on when a template is supplied (legacy) or the flag is set.
    The deck now renders programmatically, so no template file is required."""
    if template_path and os.path.exists(template_path):
        return True
    return os.environ.get("SCORING_PPTX_ENABLED", "").strip().lower() in ("1", "true", "yes")


def _merge(project_id, focal_vendor, scheme, proj: ProjectDocs, generated, extraction: ExtractionResult, warnings):
    proposal_vendors = proj.vendors()
    vendor_names: List[str] = list(proposal_vendors)

    # fold scoresheet vendors into proposal vendors (or add as scoresheet-only)
    extracted_for: Dict[str, Dict[str, ScoreCell]] = {}
    for ext_name, cells in extraction.by_vendor.items():
        match = _match_vendor(ext_name, proposal_vendors)
        target = match or ext_name
        if target not in vendor_names:
            vendor_names.append(target)
        extracted_for[target] = cells

    vendors: List[VendorResult] = []
    for name in vendor_names:
        cells: Dict[str, ScoreCell] = {}
        # extracted first (authoritative)
        for dim_id, cell in extracted_for.get(name, {}).items():
            c = ScoreCell.from_dict(cell.to_dict())
            c.vendor = name
            cells[dim_id] = c
        # then generated for remaining dims
        for dim_id, cell in (generated.get(name) or {}).items():
            if dim_id not in cells:
                c = ScoreCell.from_dict(cell.to_dict())
                c.vendor = name
                cells[dim_id] = c
        vendors.append(VendorResult(vendor=name, cells=cells))

    if extraction.by_vendor and generated:
        warnings.append(
            "Scores mix extracted (official) and generated (predicted) values; "
            "ranking uses the normalized comparison score."
        )
    return ScorecardResult(
        project_id=project_id, focal_vendor=focal_vendor, scheme=scheme,
        vendors=vendors, warnings=warnings,
    )


def _render(result, *, out_dir, out_bucket, out_prefix, template_path, s3,
            pptx_enabled=False, deck_content=None) -> ScoringOutput:
    workdir = out_dir or tempfile.mkdtemp(prefix="scoring_out_")
    os.makedirs(workdir, exist_ok=True)
    json_path = os.path.join(workdir, "scorecard.json")
    xlsx_path = os.path.join(workdir, "scorecard.xlsx")
    render_json(result, json_path)
    render_excel(result, xlsx_path)
    local: Dict[str, str] = {"json": json_path, "xlsx": xlsx_path}

    if pptx_enabled:
        try:
            from .render_pptx import render_pptx

            pptx_path = os.path.join(workdir, "scorecard.pptx")
            render_pptx(result, pptx_path, template_path=template_path, deck_content=deck_content)
            local["pptx"] = pptx_path
        except Exception as exc:  # noqa: BLE001
            result.warnings.append(f"PPTX render skipped ({exc}).")
    else:
        result.warnings.append("PowerPoint output disabled (set SCORING_PPTX_ENABLED=1 to enable).")

    if out_bucket:
        if s3 is None:
            import boto3

            s3 = boto3.client("s3")
        prefix = (out_prefix or f"outputs/{result.project_id}/").rstrip("/") + "/"
        artifacts: Dict[str, str] = {}
        for kind, path in local.items():
            key = prefix + os.path.basename(path)
            s3.upload_file(path, out_bucket, key)
            artifacts[kind] = key
        return ScoringOutput(result=result, artifacts=artifacts)

    return ScoringOutput(result=result, artifacts=local)
