"""Aggregation + competitive-intelligence layer.

Pure-Python (stdlib only), so it is fully unit-testable offline with synthetic
scorecards. Takes a :class:`ScorecardResult` whose cells already carry native
scores (from extract or generate) and:

* normalizes every cell to 0-100% (for cross-scheme comparison),
* computes each vendor's native total (when the scheme supports points) and
  normalized total,
* applies minimum-requirement gates (disqualification),
* ranks vendors,
* builds the IBM-focused :class:`CIInsight`.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .models import (
    CIInsight,
    Provenance,
    ScaleSpec,
    ScoreCell,
    ScorecardResult,
    ScoreMethod,
    SchemeSpec,
    VendorResult,
)


# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #
def _anchor_score(native_value: Optional[str], scale: ScaleSpec) -> Optional[float]:
    """Map an adjectival label (e.g. "Good") back to its numeric anchor."""
    if not native_value:
        return None
    label = native_value.strip().lower()
    for score_str, anchor in scale.anchors.items():
        # anchors may be "Good — fully meets..."; match the leading word(s)
        anchor_head = anchor.split("—")[0].split("-")[0].strip().lower()
        if label == anchor_head or label in anchor.lower():
            try:
                return float(score_str)
            except ValueError:
                continue
    return None


def normalized_pct(cell: ScoreCell, scale: ScaleSpec) -> Optional[float]:
    """Return a 0-100 comparison score for a cell, or None if unscored."""
    if cell.provenance == Provenance.GATE_FAIL:
        return 0.0
    value = cell.value
    if value is None:
        value = _anchor_score(cell.native_value, scale)
    if value is None:
        return None
    span = (scale.max - scale.min) or 1.0
    frac = (value - scale.min) / span
    if not scale.higher_is_better:
        frac = 1.0 - frac
    return max(0.0, min(1.0, frac)) * 100.0


# --------------------------------------------------------------------------- #
# Vendor totals
# --------------------------------------------------------------------------- #
def _vendor_totals(
    vendor: VendorResult, scheme: SchemeSpec, weights: Dict[str, float]
) -> Tuple[Optional[float], Optional[float]]:
    """Return (native_total, normalized_total_pct) for one vendor.

    Missing/unscored cells are excluded and the present weights are renormalized
    so an absent dimension doesn't unfairly zero the total.
    """
    weighted_sum = 0.0
    weight_present = 0.0
    points_total = 0.0
    have_points = scheme.total_max_points is not None or all(
        d.max_points is not None for d in scheme.dimensions
    )

    for dim in scheme.dimensions:
        cell = vendor.cells.get(dim.id)
        if cell is None or cell.normalized_pct is None:
            continue
        w = weights.get(dim.id, 0.0)
        weighted_sum += w * cell.normalized_pct
        weight_present += w
        if have_points and dim.max_points is not None:
            points_total += (cell.normalized_pct / 100.0) * dim.max_points

    if weight_present <= 0:
        return None, None

    normalized_total = weighted_sum / weight_present

    if scheme.total_max_points is not None:
        native_total: Optional[float] = (normalized_total / 100.0) * scheme.total_max_points
    elif have_points:
        native_total = points_total
    else:
        native_total = None

    return native_total, normalized_total


def _subset_pct(
    vendor: VendorResult, scheme: SchemeSpec, weights: Dict[str, float], dim_ids: set
) -> Optional[float]:
    """Weighted normalized % over a subset of dimensions (weights renormalized)."""
    weighted_sum = 0.0
    weight_present = 0.0
    for dim in scheme.dimensions:
        if dim.id not in dim_ids:
            continue
        cell = vendor.cells.get(dim.id)
        if cell is None or cell.normalized_pct is None:
            continue
        w = weights.get(dim.id, 0.0)
        weighted_sum += w * cell.normalized_pct
        weight_present += w
    return (weighted_sum / weight_present) if weight_present > 0 else None


def _rank_by(vendors: List[VendorResult], value, attr: str) -> None:
    """Assign 1-based ranks (desc) by ``value(v)`` to non-DQ vendors; None otherwise."""
    rankable = [v for v in vendors if not v.disqualified and value(v) is not None]
    rankable.sort(key=value, reverse=True)
    for i, v in enumerate(rankable, start=1):
        setattr(v, attr, i)
    for v in vendors:
        if v.disqualified or value(v) is None:
            setattr(v, attr, None)


# --------------------------------------------------------------------------- #
# Main entry
# --------------------------------------------------------------------------- #
def aggregate(result: ScorecardResult) -> ScorecardResult:
    """Compute normalized scores, totals, gates, ranks, and CI insights.

    Mutates and returns ``result``.
    """
    scheme = result.scheme
    scale = scheme.scale
    weights = scheme.effective_weights()
    gate_dims = {d.id for d in scheme.dimensions if d.mandatory_gate}

    # 1. normalize cells + apply gates
    for vendor in result.vendors:
        for cell in vendor.cells.values():
            cell.normalized_pct = normalized_pct(cell, scale)
        failed_gate = next(
            (
                cell
                for dim_id, cell in vendor.cells.items()
                if dim_id in gate_dims and cell.provenance == Provenance.GATE_FAIL
            ),
            None,
        )
        if failed_gate is not None:
            vendor.disqualified = True
            dim = scheme.get_dimension(failed_gate.dimension_id)
            dim_name = dim.name if dim else failed_gate.dimension_id
            vendor.disqualification_reason = (
                f"Failed minimum requirement: {dim_name}"
                + (f" — {failed_gate.rationale}" if failed_gate.rationale else "")
            )

    # 2. totals
    for vendor in result.vendors:
        native_total, normalized_total = _vendor_totals(vendor, scheme, weights)
        vendor.native_total = native_total
        vendor.normalized_total_pct = normalized_total

    # 3. rank (non-disqualified, by normalized total desc)
    _rank_by(result.vendors, lambda v: v.normalized_total_pct, "rank")

    # 3b. technical (non-cost) vs financial (cost) split + their own ranks.
    # Feeds the deck's "Final Scoring" slide; financial_* stay None when the
    # scheme has no cost-related dimension.
    cost_dims = {d.id for d in scheme.dimensions if d.cost_related}
    tech_dims = {d.id for d in scheme.dimensions if not d.cost_related}
    for vendor in result.vendors:
        vendor.technical_pct = _subset_pct(vendor, scheme, weights, tech_dims)
        vendor.financial_pct = _subset_pct(vendor, scheme, weights, cost_dims) if cost_dims else None
    _rank_by(result.vendors, lambda v: v.technical_pct, "technical_rank")
    if cost_dims:
        _rank_by(result.vendors, lambda v: v.financial_pct, "financial_rank")

    # 4. CI insights
    result.ci = _build_ci(result, weights)
    return result


# --------------------------------------------------------------------------- #
# Competitive-intelligence layer
# --------------------------------------------------------------------------- #
def _find_focal(result: ScorecardResult) -> Optional[VendorResult]:
    focal = result.focal_vendor.strip().lower()
    if not focal:
        return None
    # exact, then substring (e.g. "IBM" matches "IBM Corporation")
    for v in result.vendors:
        if v.vendor.strip().lower() == focal:
            return v
    for v in result.vendors:
        if focal in v.vendor.strip().lower():
            return v
    return None


def _dim_leader_pct(result: ScorecardResult, dim_id: str) -> Optional[float]:
    vals = [
        v.cells[dim_id].normalized_pct
        for v in result.vendors
        if dim_id in v.cells and v.cells[dim_id].normalized_pct is not None and not v.disqualified
    ]
    return max(vals) if vals else None


def _build_ci(result: ScorecardResult, weights: Dict[str, float]) -> Optional[CIInsight]:
    focal = _find_focal(result)
    if focal is None:
        result.warnings.append(
            f"Focal vendor '{result.focal_vendor}' not found among bidders; CI insights skipped."
        )
        return None

    scheme = result.scheme
    ranked = result.ranked_vendors()
    top = ranked[0] if ranked and not ranked[0].disqualified else None

    ci = CIInsight(focal_vendor=focal.vendor, rank=focal.rank)
    if top is not None:
        ci.top_vendor = top.vendor
        if (
            focal.normalized_total_pct is not None
            and top.normalized_total_pct is not None
        ):
            ci.gap_to_top_pct = round(top.normalized_total_pct - focal.normalized_total_pct, 1)

    # per-dimension standing
    drivers: List[Tuple[float, str]] = []   # (weighted gap, dim name)
    for dim in scheme.dimensions:
        cell = focal.cells.get(dim.id)
        if cell is None or cell.normalized_pct is None:
            continue
        leader_pct = _dim_leader_pct(result, dim.id)
        if leader_pct is None:
            continue
        w = weights.get(dim.id, 0.0)
        if cell.normalized_pct >= leader_pct - 1e-6:
            ci.strengths.append(dim.name)
        else:
            gap = leader_pct - cell.normalized_pct
            ci.weaknesses.append(dim.name)
            drivers.append((w * gap, dim.name))

    # biggest rank drivers: dims where focal loses the most weighted ground
    drivers.sort(reverse=True)
    ci.key_drivers = [name for _, name in drivers[:3]]

    ci.summary = _ci_summary(ci, focal, top, len(result.vendors), scheme.method)
    return ci


def _ci_summary(
    ci: CIInsight,
    focal: VendorResult,
    top: Optional[VendorResult],
    n_vendors: int,
    method: ScoreMethod,
) -> str:
    rank_txt = f"#{ci.rank} of {n_vendors}" if ci.rank else "unranked"
    focal_pct = focal.normalized_total_pct
    parts = [f"{focal.vendor} ranks {rank_txt}"]
    if focal_pct is not None:
        parts[0] += f" ({focal_pct:.0f}% normalized)"
    if top is not None and top.vendor != focal.vendor and top.normalized_total_pct is not None:
        parts.append(
            f"behind {top.vendor} ({top.normalized_total_pct:.0f}%), "
            f"a {ci.gap_to_top_pct:.0f}-point gap"
        )
    elif top is not None and top.vendor == focal.vendor:
        parts.append("the top-ranked bid")
    if ci.key_drivers:
        parts.append("biggest gaps: " + ", ".join(ci.key_drivers))
    if ci.strengths:
        parts.append("leads on: " + ", ".join(ci.strengths[:3]))
    return "; ".join(parts) + "."
