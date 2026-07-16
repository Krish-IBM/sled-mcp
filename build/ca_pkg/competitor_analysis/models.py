"""Data structures for the competitor-analysis pipeline."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

# The analysis dimensions, in report order. Descriptions are fed verbatim into
# the digest and synthesis prompts, so keep them concrete about what evidence
# to look for.
DIMENSIONS: Dict[str, Dict[str, str]] = {
    "solutioning": {
        "title": "Solutioning & Technical Approach",
        "description": (
            "How the competitor architects and positions its solution: platforms and "
            "products proposed (COTS vs custom, cloud vs on-prem), delivery methodology "
            "(agile/waterfall/hybrid), implementation phasing, reuse of accelerators or "
            "prior-state assets, and how they respond to technical requirements."
        ),
    },
    "staffing": {
        "title": "Staffing & Key Personnel",
        "description": (
            "How the competitor staffs bids: team size and structure, onshore/offshore "
            "mix, named key personnel and their credentials, use of subcontractors for "
            "staffing, staffing ramp plans, and rate-card roles."
        ),
    },
    "pricing": {
        "title": "Pricing & Commercial Strategy",
        "description": (
            "The competitor's pricing behavior: total bid values, rate cards, fixed-price "
            "vs T&M structures, how they load cost into phases (e.g. low implementation / "
            "high M&O), discounts, assumptions, and where they win or lose on cost."
        ),
    },
    "past_performance": {
        "title": "Past Performance & References",
        "description": (
            "Which contracts, clients, and references the competitor cites; the delivery "
            "track record they claim; which prior state/local projects they showcase and "
            "how evaluators scored their experience."
        ),
    },
    "win_themes": {
        "title": "Win Themes & Differentiators",
        "description": (
            "The recurring messages the competitor leads with to win: claimed "
            "differentiators, value propositions, risk-reduction promises, incumbency "
            "arguments, local presence, and how they position against rivals."
        ),
    },
}


@dataclass
class ProcurementDigest:
    """Per-procurement evidence summary produced by one fast-model pass."""

    procurement: str                    # folder name under the vendor prefix
    client: str = ""                    # issuing agency/state, if identifiable
    year: str = ""
    outcome: str = ""                   # won / lost / unknown (+ brief basis)
    dimension_notes: Dict[str, str] = field(default_factory=dict)  # dim key -> findings
    source_docs: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class EvidenceItem:
    procurement: str
    detail: str                         # the specific fact + source doc when known


@dataclass
class DimensionFinding:
    key: str
    title: str
    analysis: str                       # synthesized cross-procurement narrative
    evidence: List[EvidenceItem] = field(default_factory=list)
    ibm_implications: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CompetitorAnalysis:
    competitor: str                     # resolved vendor folder name
    focal: str = "IBM"
    executive_summary: str = ""
    dimensions: List[DimensionFinding] = field(default_factory=list)
    procurement_digests: List[ProcurementDigest] = field(default_factory=list)
    docs_analyzed: int = 0
    docs_skipped: int = 0
    warnings: List[str] = field(default_factory=list)
    generated_at: str = field(
        default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "competitor": self.competitor,
            "focal": self.focal,
            "generated_at": self.generated_at,
            "executive_summary": self.executive_summary,
            "dimensions": [d.to_dict() for d in self.dimensions],
            "procurements_analyzed": [p.to_dict() for p in self.procurement_digests],
            "docs_analyzed": self.docs_analyzed,
            "docs_skipped": self.docs_skipped,
            "warnings": self.warnings,
        }
