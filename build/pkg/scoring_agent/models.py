"""Core data models for the SLED bid-scoring agent.

Pure-Python (stdlib only) so it is unit-testable offline without AWS/Bedrock.
Everything serializes to plain dicts via ``to_dict`` for the canonical JSON
output and for handing structured data to the renderers.

Key concepts
------------
* ``SchemeSpec``  - the *native* scoring scheme parsed from a specific RFP
  (weighted-points like WA CCWIS, best-value trade-off like NC, adjectival,
  or pass/fail + points). We mirror the native scheme for fidelity and also
  derive a normalized 0-100% comparison score so vendors remain comparable
  across differing schemes.
* ``ScoreCell``   - one vendor's score on one dimension, tagged with provenance
  (``extracted`` from a real scoresheet vs ``generated`` by Claude) and, for
  generated cells, the proposal evidence + rationale behind it.
* ``VendorResult`` / ``ScorecardResult`` - the assembled matrix + ranking.
* ``CIInsight``   - the competitive-intelligence layer focused on IBM.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class ScoreMethod(str, Enum):
    """How a solicitation converts judgments into an award decision."""

    WEIGHTED_POINTS = "weighted_points"          # WA CCWIS: 1-5 -> % -> weighted pts
    BEST_VALUE_TRADEOFF = "best_value_tradeoff"  # NC: priority-ranked qualitative
    ADJECTIVAL = "adjectival"                    # Excellent/Good/Marginal/Poor
    PASS_FAIL_PLUS_POINTS = "pass_fail_plus_points"


class Provenance(str, Enum):
    """Where a score came from."""

    EXTRACTED = "extracted"      # pulled from a real (FOIA) scoresheet
    GENERATED = "generated"      # predicted by Claude from the proposal
    GATE_FAIL = "gate_fail"      # failed a minimum-requirement gate
    NOT_SCORED = "not_scored"    # no evidence / not applicable


def _enum_val(v: Any) -> Any:
    return v.value if isinstance(v, Enum) else v


# --------------------------------------------------------------------------- #
# Scheme / rubric structures
# --------------------------------------------------------------------------- #
@dataclass
class ScaleSpec:
    """The scale a scheme scores each dimension on."""

    type: str = "numeric"                        # "numeric" | "adjectival"
    min: float = 1.0
    max: float = 5.0
    higher_is_better: bool = True
    # score value -> plain-language anchor, e.g. {"1": "Poor", "5": "Excellent"}
    anchors: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "min": self.min,
            "max": self.max,
            "higher_is_better": self.higher_is_better,
            "anchors": dict(self.anchors),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ScaleSpec":
        return cls(
            type=d.get("type", "numeric"),
            min=float(d.get("min", 1.0)),
            max=float(d.get("max", 5.0)),
            higher_is_better=bool(d.get("higher_is_better", True)),
            anchors={str(k): str(v) for k, v in (d.get("anchors") or {}).items()},
        )


@dataclass
class Dimension:
    """One evaluation dimension (a scorecard row)."""

    id: str
    name: str
    description: str = ""
    weight: Optional[float] = None          # fraction 0-1 (weighted-points schemes)
    priority: Optional[int] = None          # 1 = most important (best-value schemes)
    max_points: Optional[float] = None      # native max points for this dimension
    sub_criteria: List[str] = field(default_factory=list)
    mandatory_gate: bool = False            # failing this disqualifies the vendor
    cost_related: bool = False              # price/cost dimension

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "weight": self.weight,
            "priority": self.priority,
            "max_points": self.max_points,
            "sub_criteria": list(self.sub_criteria),
            "mandatory_gate": self.mandatory_gate,
            "cost_related": self.cost_related,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Dimension":
        return cls(
            id=str(d["id"]),
            name=str(d.get("name", d["id"])),
            description=str(d.get("description", "")),
            weight=None if d.get("weight") is None else float(d["weight"]),
            priority=None if d.get("priority") is None else int(d["priority"]),
            max_points=None if d.get("max_points") is None else float(d["max_points"]),
            sub_criteria=list(d.get("sub_criteria") or []),
            mandatory_gate=bool(d.get("mandatory_gate", False)),
            cost_related=bool(d.get("cost_related", False)),
        )


@dataclass
class SchemeSpec:
    """The native scoring scheme for a single procurement."""

    project_id: str
    method: ScoreMethod = ScoreMethod.WEIGHTED_POINTS
    scale: ScaleSpec = field(default_factory=ScaleSpec)
    dimensions: List[Dimension] = field(default_factory=list)
    cost_handling: str = "dimension"        # "dimension" | "tradeoff"
    cost_formula: Optional[str] = None
    gates: List[str] = field(default_factory=list)   # minimum-requirement gates
    total_max_points: Optional[float] = None
    source: str = "base_rubric"             # "parsed_from_rfp" | "base_rubric" | "scoresheet"
    notes: str = ""

    # -- lookups ---------------------------------------------------------- #
    def get_dimension(self, dim_id: str) -> Optional[Dimension]:
        for d in self.dimensions:
            if d.id == dim_id:
                return d
        return None

    def effective_weights(self) -> Dict[str, float]:
        """Normalized weights (sum ~= 1.0) used for the comparison score.

        Priority order of derivation:
        1. explicit ``weight`` on every dimension -> normalize.
        2. ``max_points`` on every dimension -> normalize by points share.
        3. ``priority`` ranks (best-value schemes) -> rank-decreasing weights.
        4. otherwise -> equal weights.

        This drives only the *normalized* comparison score; the native scheme
        representation is preserved separately.
        """
        dims = self.dimensions
        if not dims:
            return {}

        if all(d.weight is not None for d in dims):
            total = sum(d.weight for d in dims) or 1.0
            return {d.id: (d.weight / total) for d in dims}

        if all(d.max_points is not None for d in dims):
            total = sum(d.max_points for d in dims) or 1.0
            return {d.id: (d.max_points / total) for d in dims}

        if all(d.priority is not None for d in dims):
            # Rank-decreasing weights: rank 1 gets the largest share.
            n = len(dims)
            raw = {d.id: (n - d.priority + 1) for d in dims}
            total = sum(raw.values()) or 1.0
            return {k: v / total for k, v in raw.items()}

        equal = 1.0 / len(dims)
        return {d.id: equal for d in dims}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_id": self.project_id,
            "method": _enum_val(self.method),
            "scale": self.scale.to_dict(),
            "dimensions": [d.to_dict() for d in self.dimensions],
            "cost_handling": self.cost_handling,
            "cost_formula": self.cost_formula,
            "gates": list(self.gates),
            "total_max_points": self.total_max_points,
            "source": self.source,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SchemeSpec":
        return cls(
            project_id=str(d.get("project_id", "")),
            method=ScoreMethod(d.get("method", ScoreMethod.WEIGHTED_POINTS.value)),
            scale=ScaleSpec.from_dict(d.get("scale") or {}),
            dimensions=[Dimension.from_dict(x) for x in (d.get("dimensions") or [])],
            cost_handling=d.get("cost_handling", "dimension"),
            cost_formula=d.get("cost_formula"),
            gates=list(d.get("gates") or []),
            total_max_points=d.get("total_max_points"),
            source=d.get("source", "base_rubric"),
            notes=d.get("notes", ""),
        )


# --------------------------------------------------------------------------- #
# Score structures
# --------------------------------------------------------------------------- #
@dataclass
class Evidence:
    """A citation supporting a generated score."""

    doc: str                        # source document name/key
    quote: str                      # short verbatim excerpt
    page: Optional[int] = None
    locator: Optional[str] = None   # section heading or other locator

    def to_dict(self) -> Dict[str, Any]:
        return {"doc": self.doc, "quote": self.quote, "page": self.page, "locator": self.locator}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Evidence":
        return cls(
            doc=str(d.get("doc", "")),
            quote=str(d.get("quote", "")),
            page=d.get("page"),
            locator=d.get("locator"),
        )


@dataclass
class ScoreCell:
    """One vendor's score on one dimension."""

    vendor: str
    dimension_id: str
    value: Optional[float] = None            # native numeric score (if numeric scale)
    native_value: Optional[str] = None       # native label (adjectival / points string)
    normalized_pct: Optional[float] = None   # 0-100, for cross-scheme comparison
    provenance: Provenance = Provenance.NOT_SCORED
    confidence: Optional[float] = None       # 0-1 (generated cells)
    rationale: str = ""
    evidence: List[Evidence] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "vendor": self.vendor,
            "dimension_id": self.dimension_id,
            "value": self.value,
            "native_value": self.native_value,
            "normalized_pct": self.normalized_pct,
            "provenance": _enum_val(self.provenance),
            "confidence": self.confidence,
            "rationale": self.rationale,
            "evidence": [e.to_dict() for e in self.evidence],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ScoreCell":
        return cls(
            vendor=str(d.get("vendor", "")),
            dimension_id=str(d.get("dimension_id", "")),
            value=d.get("value"),
            native_value=d.get("native_value"),
            normalized_pct=d.get("normalized_pct"),
            provenance=Provenance(d.get("provenance", Provenance.NOT_SCORED.value)),
            confidence=d.get("confidence"),
            rationale=d.get("rationale", ""),
            evidence=[Evidence.from_dict(x) for x in (d.get("evidence") or [])],
        )


@dataclass
class VendorResult:
    """All of one vendor's cells plus their totals/rank."""

    vendor: str
    cells: Dict[str, ScoreCell] = field(default_factory=dict)  # dimension_id -> cell
    native_total: Optional[float] = None
    normalized_total_pct: Optional[float] = None
    rank: Optional[int] = None
    disqualified: bool = False
    disqualification_reason: Optional[str] = None
    # Technical (non-cost) vs financial (cost) split — drives the deck's
    # "Final Scoring" slide (Technical Rank / Financial Rank). financial_* stay
    # None when the scheme has no cost-related dimension.
    technical_pct: Optional[float] = None
    financial_pct: Optional[float] = None
    technical_rank: Optional[int] = None
    financial_rank: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "vendor": self.vendor,
            "cells": {k: c.to_dict() for k, c in self.cells.items()},
            "native_total": self.native_total,
            "normalized_total_pct": self.normalized_total_pct,
            "rank": self.rank,
            "disqualified": self.disqualified,
            "disqualification_reason": self.disqualification_reason,
            "technical_pct": self.technical_pct,
            "financial_pct": self.financial_pct,
            "technical_rank": self.technical_rank,
            "financial_rank": self.financial_rank,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "VendorResult":
        return cls(
            vendor=str(d.get("vendor", "")),
            cells={k: ScoreCell.from_dict(v) for k, v in (d.get("cells") or {}).items()},
            native_total=d.get("native_total"),
            normalized_total_pct=d.get("normalized_total_pct"),
            rank=d.get("rank"),
            disqualified=bool(d.get("disqualified", False)),
            disqualification_reason=d.get("disqualification_reason"),
            technical_pct=d.get("technical_pct"),
            financial_pct=d.get("financial_pct"),
            technical_rank=d.get("technical_rank"),
            financial_rank=d.get("financial_rank"),
        )


@dataclass
class CIInsight:
    """Competitive-intelligence summary centered on the focal vendor (IBM)."""

    focal_vendor: str
    rank: Optional[int] = None
    top_vendor: Optional[str] = None
    gap_to_top_pct: Optional[float] = None
    strengths: List[str] = field(default_factory=list)      # dims where focal leads
    weaknesses: List[str] = field(default_factory=list)     # dims where focal trails
    key_drivers: List[str] = field(default_factory=list)    # biggest rank drivers
    summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "focal_vendor": self.focal_vendor,
            "rank": self.rank,
            "top_vendor": self.top_vendor,
            "gap_to_top_pct": self.gap_to_top_pct,
            "strengths": list(self.strengths),
            "weaknesses": list(self.weaknesses),
            "key_drivers": list(self.key_drivers),
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CIInsight":
        return cls(
            focal_vendor=str(d.get("focal_vendor", "")),
            rank=d.get("rank"),
            top_vendor=d.get("top_vendor"),
            gap_to_top_pct=d.get("gap_to_top_pct"),
            strengths=list(d.get("strengths") or []),
            weaknesses=list(d.get("weaknesses") or []),
            key_drivers=list(d.get("key_drivers") or []),
            summary=d.get("summary", ""),
        )


@dataclass
class ScorecardResult:
    """The complete scoring result for one project."""

    project_id: str
    focal_vendor: str
    scheme: SchemeSpec
    vendors: List[VendorResult] = field(default_factory=list)
    ci: Optional[CIInsight] = None
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    warnings: List[str] = field(default_factory=list)

    def vendor_names(self) -> List[str]:
        return [v.vendor for v in self.vendors]

    def ranked_vendors(self) -> List[VendorResult]:
        """Vendors ordered by rank (unranked/disqualified last)."""
        return sorted(
            self.vendors,
            key=lambda v: (v.disqualified, v.rank if v.rank is not None else 10_000),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_id": self.project_id,
            "focal_vendor": self.focal_vendor,
            "scheme": self.scheme.to_dict(),
            "vendors": [v.to_dict() for v in self.vendors],
            "ci": self.ci.to_dict() if self.ci else None,
            "generated_at": self.generated_at,
            "warnings": list(self.warnings),
            "schema_version": "1.0",
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ScorecardResult":
        return cls(
            project_id=str(d.get("project_id", "")),
            focal_vendor=str(d.get("focal_vendor", "")),
            scheme=SchemeSpec.from_dict(d.get("scheme") or {}),
            vendors=[VendorResult.from_dict(x) for x in (d.get("vendors") or [])],
            ci=CIInsight.from_dict(d["ci"]) if d.get("ci") else None,
            generated_at=d.get("generated_at", ""),
            warnings=list(d.get("warnings") or []),
        )
