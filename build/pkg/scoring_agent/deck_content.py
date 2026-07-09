"""Narrative + metadata content for the PowerPoint deck.

The single-table scorecard (JSON/XLSX) is fully deterministic. The competitive
deck, however, needs a few pieces of prose the raw scores don't carry:

* procurement metadata  (agency, RFP #, total contract value, a plain summary),
* outcome drivers        (why the winner won / why the focal vendor lost),
* a category comparison  (focal vs. winner on Price / Implementation / Testing /
  Solution Differentiators).

Each builder makes one bounded Bedrock call and **fails soft** — on any error it
appends a warning and returns an empty/derived default so the deck (and the rest
of the pipeline) still renders. These only run when PPTX output is enabled, so
the extra Claude calls are not incurred on JSON/XLSX-only runs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .ingest import DocumentText, ProjectDocs
from .models import ScorecardResult, VendorResult

# Fixed category set mirrored from the reference decks.
CATEGORIES = ("Price", "Implementation Plan", "Testing", "Solution Differentiators")

_META_SYSTEM = (
    "You extract structured metadata from a U.S. state or local government "
    "solicitation (RFP/RFQ). Report only what the text states."
)
_DRIVERS_SYSTEM = (
    "You are a competitive-intelligence analyst explaining a government "
    "source-selection outcome through the focal vendor's lens. Ground every "
    "statement in the provided scores; be candid about the focal vendor's gaps."
)
_COMPARE_SYSTEM = (
    "You compare two vendors' proposals for a competitive-intelligence deck. "
    "Use only the provided excerpts; keep each point to a short phrase."
)


# --------------------------------------------------------------------------- #
# Content dataclasses
# --------------------------------------------------------------------------- #
@dataclass
class ProcurementMeta:
    agency: str = ""
    rfp_number: str = ""
    tcv: str = ""
    summary: str = ""
    winning_vendor: str = ""
    vendors: List[str] = field(default_factory=list)
    documents: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agency": self.agency,
            "rfp_number": self.rfp_number,
            "tcv": self.tcv,
            "summary": self.summary,
            "winning_vendor": self.winning_vendor,
            "vendors": list(self.vendors),
            "documents": list(self.documents),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ProcurementMeta":
        return cls(
            agency=str(d.get("agency", "")),
            rfp_number=str(d.get("rfp_number", "")),
            tcv=str(d.get("tcv", "")),
            summary=str(d.get("summary", "")),
            winning_vendor=str(d.get("winning_vendor", "")),
            vendors=list(d.get("vendors") or []),
            documents=list(d.get("documents") or []),
        )


@dataclass
class DriverRow:
    factor: str          # winning factor / issue area
    evidence: str        # evidence from scoring
    impact: str          # why it mattered / impact

    def to_dict(self) -> Dict[str, Any]:
        return {"factor": self.factor, "evidence": self.evidence, "impact": self.impact}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DriverRow":
        return cls(
            factor=str(d.get("factor", "")),
            evidence=str(d.get("evidence", "")),
            impact=str(d.get("impact", "")),
        )


@dataclass
class OutcomeDrivers:
    winner: str = ""
    focal: str = ""
    why_won: List[DriverRow] = field(default_factory=list)
    why_focal_lost: List[DriverRow] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "winner": self.winner,
            "focal": self.focal,
            "why_won": [r.to_dict() for r in self.why_won],
            "why_focal_lost": [r.to_dict() for r in self.why_focal_lost],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "OutcomeDrivers":
        return cls(
            winner=str(d.get("winner", "")),
            focal=str(d.get("focal", "")),
            why_won=[DriverRow.from_dict(x) for x in (d.get("why_won") or [])],
            why_focal_lost=[DriverRow.from_dict(x) for x in (d.get("why_focal_lost") or [])],
        )


@dataclass
class CategoryRow:
    category: str
    focal_points: List[str] = field(default_factory=list)
    winner_points: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "focal_points": list(self.focal_points),
            "winner_points": list(self.winner_points),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CategoryRow":
        return cls(
            category=str(d.get("category", "")),
            focal_points=list(d.get("focal_points") or []),
            winner_points=list(d.get("winner_points") or []),
        )


@dataclass
class CategoryComparison:
    focal: str = ""
    winner: str = ""
    rows: List[CategoryRow] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "focal": self.focal,
            "winner": self.winner,
            "rows": [r.to_dict() for r in self.rows],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CategoryComparison":
        return cls(
            focal=str(d.get("focal", "")),
            winner=str(d.get("winner", "")),
            rows=[CategoryRow.from_dict(x) for x in (d.get("rows") or [])],
        )


@dataclass
class DeckContent:
    meta: ProcurementMeta = field(default_factory=ProcurementMeta)
    drivers: Optional[OutcomeDrivers] = None
    comparison: Optional[CategoryComparison] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "meta": self.meta.to_dict(),
            "drivers": self.drivers.to_dict() if self.drivers else None,
            "comparison": self.comparison.to_dict() if self.comparison else None,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DeckContent":
        return cls(
            meta=ProcurementMeta.from_dict(d.get("meta") or {}),
            drivers=OutcomeDrivers.from_dict(d["drivers"]) if d.get("drivers") else None,
            comparison=CategoryComparison.from_dict(d["comparison"]) if d.get("comparison") else None,
        )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _pct(v: Optional[float]) -> str:
    return f"{v:.0f}%" if v is not None else "—"


def resolve_winner(result: ScorecardResult) -> Optional[VendorResult]:
    """The awarded/top-ranked vendor (rank #1, not disqualified)."""
    ranked = result.ranked_vendors()
    return ranked[0] if ranked and not ranked[0].disqualified else None


def resolve_focal(result: ScorecardResult) -> Optional[VendorResult]:
    focal = result.focal_vendor.strip().lower()
    if not focal:
        return None
    for v in result.vendors:                       # exact, then substring
        if v.vendor.strip().lower() == focal:
            return v
    for v in result.vendors:
        if focal in v.vendor.strip().lower():
            return v
    return None


def _scores_digest(result: ScorecardResult) -> str:
    lines: List[str] = []
    for v in result.ranked_vendors():
        tag = "DQ" if v.disqualified else (f"#{v.rank}" if v.rank else "—")
        parts = [f'"{v.vendor}" [{tag}] overall={_pct(v.normalized_total_pct)}']
        if v.technical_pct is not None:
            parts.append(f"technical={_pct(v.technical_pct)}")
        if v.financial_pct is not None:
            parts.append(f"financial={_pct(v.financial_pct)}")
        for dim in result.scheme.dimensions:
            cell = v.cells.get(dim.id)
            if cell is not None and cell.normalized_pct is not None:
                native = f"/{cell.native_value}" if cell.native_value else ""
                parts.append(f"{dim.name}={_pct(cell.normalized_pct)}{native}")
        lines.append("; ".join(parts))
    return "\n".join(lines)


def _proposal_digest(proposal_texts: Dict[str, List[DocumentText]], vendor: str, cap: int = 6000) -> str:
    dts = proposal_texts.get(vendor) or []
    text = "\n\n".join(dt.full_text for dt in dts if dt.pages)
    return text[:cap]


def _client(bedrock):
    if bedrock is not None:
        return bedrock
    from .bedrock import default_client

    return default_client()


# --------------------------------------------------------------------------- #
# Builders (each fails soft)
# --------------------------------------------------------------------------- #
def build_procurement_meta(
    result: ScorecardResult,
    proj: Optional[ProjectDocs],
    rfp_text: str,
    *,
    bedrock=None,
    warnings: Optional[List[str]] = None,
) -> ProcurementMeta:
    warnings = warnings if warnings is not None else []
    winner = resolve_winner(result)
    meta = ProcurementMeta(
        winning_vendor=winner.vendor if winner else "",
        vendors=[v.vendor for v in result.vendors],
        documents=[d.id for d in proj.all_documents()] if proj else [],
    )

    if not rfp_text.strip():
        return meta

    prompt = (
        'Extract this JSON from the solicitation (use "" for any field not stated):\n'
        '{"agency": "...", "rfp_number": "...", "tcv": "...", "summary": "..."}\n'
        "- agency: the procuring government agency/department.\n"
        "- rfp_number: the RFP/RFQ/solicitation number.\n"
        "- tcv: total/estimated contract value with currency, if stated.\n"
        "- summary: 1-2 plain sentences on what is being procured.\n\n"
        "SOLICITATION TEXT:\n" + rfp_text[:12000]
    )
    try:
        data = _client(bedrock).converse_json(prompt, system=_META_SYSTEM, max_tokens=800, fast=True)
        if isinstance(data, dict):
            meta.agency = str(data.get("agency", "")).strip()
            meta.rfp_number = str(data.get("rfp_number", "")).strip()
            meta.tcv = str(data.get("tcv", "")).strip()
            meta.summary = str(data.get("summary", "")).strip()
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Procurement metadata extraction skipped ({exc}).")
    return meta


def build_outcome_drivers(
    result: ScorecardResult,
    *,
    bedrock=None,
    warnings: Optional[List[str]] = None,
) -> Optional[OutcomeDrivers]:
    warnings = warnings if warnings is not None else []
    winner = resolve_winner(result)
    focal = resolve_focal(result)
    if winner is None or focal is None:
        return None

    prompt = (
        f'The winning vendor is "{winner.vendor}". The focal vendor is "{focal.vendor}".\n'
        "Using ONLY the scores below, return JSON:\n"
        '{"why_won": [{"factor": "...", "evidence": "...", "impact": "..."}],\n'
        ' "why_focal_lost": [{"factor": "...", "evidence": "...", "impact": "..."}]}\n'
        f"- why_won: up to 5 factors that drove {winner.vendor}'s win.\n"
        f"- why_focal_lost: up to 5 issues where {focal.vendor} fell short.\n"
        "- evidence must cite concrete numbers from the scores (e.g. 'narrative 237.5 vs 480').\n"
        "- impact = why it mattered to the outcome. Keep each field to one concise sentence.\n\n"
        "SCORES:\n" + _scores_digest(result)
    )
    try:
        data = _client(bedrock).converse_json(prompt, system=_DRIVERS_SYSTEM, max_tokens=1500)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Outcome-driver narrative skipped ({exc}).")
        return None

    if not isinstance(data, dict):
        return None
    drivers = OutcomeDrivers(winner=winner.vendor, focal=focal.vendor)
    drivers.why_won = [DriverRow.from_dict(x) for x in (data.get("why_won") or []) if isinstance(x, dict)][:5]
    drivers.why_focal_lost = [
        DriverRow.from_dict(x) for x in (data.get("why_focal_lost") or []) if isinstance(x, dict)
    ][:5]
    if not drivers.why_won and not drivers.why_focal_lost:
        return None
    return drivers


def build_category_comparison(
    result: ScorecardResult,
    proposal_texts: Dict[str, List[DocumentText]],
    *,
    bedrock=None,
    warnings: Optional[List[str]] = None,
) -> Optional[CategoryComparison]:
    warnings = warnings if warnings is not None else []
    winner = resolve_winner(result)
    focal = resolve_focal(result)
    if winner is None or focal is None or winner.vendor == focal.vendor:
        return None

    focal_digest = _proposal_digest(proposal_texts, focal.vendor)
    winner_digest = _proposal_digest(proposal_texts, winner.vendor)
    if not focal_digest and not winner_digest:
        return None

    cats = ", ".join(CATEGORIES)
    prompt = (
        f'Compare the focal vendor "{focal.vendor}" against the winner "{winner.vendor}" '
        f"across these categories: {cats}.\n"
        "Return JSON:\n"
        '{"rows": [{"category": "<one of the categories>", '
        '"focal_points": ["...", "..."], "winner_points": ["...", "..."]}]}\n'
        "- One row per category, in the order listed.\n"
        "- 2-4 short bullet points per side; use [] when a side gives no detail.\n\n"
        f"FOCAL ({focal.vendor}) PROPOSAL EXCERPTS:\n{focal_digest or '(none)'}\n\n"
        f"WINNER ({winner.vendor}) PROPOSAL EXCERPTS:\n{winner_digest or '(none)'}"
    )
    try:
        data = _client(bedrock).converse_json(prompt, system=_COMPARE_SYSTEM, max_tokens=2000)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Category comparison skipped ({exc}).")
        return None

    if not isinstance(data, dict):
        return None
    rows = [CategoryRow.from_dict(x) for x in (data.get("rows") or []) if isinstance(x, dict)]
    rows = [r for r in rows if r.category and (r.focal_points or r.winner_points)]
    if not rows:
        return None
    return CategoryComparison(focal=focal.vendor, winner=winner.vendor, rows=rows)


def build_deck_content(
    result: ScorecardResult,
    *,
    rfp_text: str = "",
    proposal_texts: Optional[Dict[str, List[DocumentText]]] = None,
    proj: Optional[ProjectDocs] = None,
    bedrock=None,
    deadline_epoch: Optional[float] = None,
    warnings: Optional[List[str]] = None,
) -> DeckContent:
    """Assemble all deck content. Deadline-aware: skips remaining Bedrock work
    (keeping what is already built) once the time budget is spent."""
    warnings = warnings if warnings is not None else []
    proposal_texts = proposal_texts or {}

    def _out_of_time() -> bool:
        return deadline_epoch is not None and time.time() > deadline_epoch

    meta = build_procurement_meta(result, proj, rfp_text, bedrock=bedrock, warnings=warnings)

    drivers = None
    comparison = None
    if _out_of_time():
        warnings.append("Deck narrative skipped — scoring time budget reached before generation.")
    else:
        drivers = build_outcome_drivers(result, bedrock=bedrock, warnings=warnings)
        if not _out_of_time():
            comparison = build_category_comparison(
                result, proposal_texts, bedrock=bedrock, warnings=warnings
            )

    return DeckContent(meta=meta, drivers=drivers, comparison=comparison)
