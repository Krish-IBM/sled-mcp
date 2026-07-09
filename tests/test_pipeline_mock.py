"""Integration test of the full pipeline with a MOCK Bedrock client.

Exercises load -> ingest -> scheme -> extract -> generate -> merge -> aggregate
-> render without any AWS calls, using tiny text documents and a fake Bedrock
whose replies are keyed off the prompt. Verifies the extract+generate merge,
scoresheet-vendor matching, and artifact production.

Run:  ./.venv/bin/python tests/test_pipeline_mock.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scoring_agent.models import Provenance
from scoring_agent.pipeline import run_scoring

class MockBedrock:
    """Fake Bedrock client; returns canned JSON based on the prompt's intent."""

    def converse(self, user_text, **kwargs):  # OCR path (unused for .txt docs)
        return ""

    def converse_json(self, user_text, **kwargs):
        if "native evaluation scheme" in user_text:
            return {
                "method": "weighted_points",
                "scale": {"type": "numeric", "min": 1, "max": 5, "higher_is_better": True,
                          "anchors": {"1": "Poor", "5": "Excellent"}},
                "cost_handling": "dimension",
                "total_max_points": 100,
                "gates": ["Must hold required state certification"],
                "dimensions": [
                    {"id": "vendor_qualifications", "name": "Vendor Qualifications & Experience",
                     "weight": 0.4, "max_points": 40},
                    {"id": "technical_approach", "name": "Technical Approach & Solution Fit",
                     "weight": 0.4, "max_points": 40},
                    {"id": "cost_price", "name": "Cost / Price", "weight": 0.2, "max_points": 20,
                     "cost_related": True},
                ],
            }
        if "final composite scores" in user_text:
            # scoresheet has an official score for Deloitte
            return {
                "vendors": [
                    {"name": "Deloitte", "total": 88.0, "scores": [
                        {"dimension_id": "vendor_qualifications", "value": 5, "native_value": "40",
                         "note": "extensive prior CCWIS work"},
                        {"dimension_id": "technical_approach", "value": 4, "native_value": "32",
                         "note": "solid architecture"},
                    ]},
                ]
            }
        if "Predict the panel's scores" in user_text:
            vendor = re.search(r'vendor "([^"]+)"', user_text)
            vendor = vendor.group(1) if vendor else "?"
            dims = re.findall(r"DIMENSION (\w+):", user_text)
            # a competitor outscores IBM so the awarded winner != focal, which
            # exercises the full 7-slide deck (incl. the category-comparison slide)
            base = 3 if vendor.lower().startswith("ibm") else 4
            scores = []
            for i, d in enumerate(dims):
                scores.append({
                    "dimension_id": d,
                    "value": max(1, min(5, base - (1 if d == "cost_price" else 0) + (i % 2))),
                    "native_value": None,
                    "rationale": f"{vendor} shows adequate coverage on {d}.",
                    "confidence": 0.7,
                    "gate_pass": True,
                    "evidence": [{"page": 1, "quote": f"…{d} details…"}],
                })
            return {"scores": scores}
        # deck-content builders (only reached when PPTX is enabled)
        if "SOLICITATION TEXT" in user_text:
            return {"agency": "State Demo Agency", "rfp_number": "RFQ-DEMO-1",
                    "tcv": "$5,800,000", "summary": "Implement a demo system."}
        if "why_won" in user_text:
            return {"why_won": [{"factor": "Stronger technical narrative",
                                 "evidence": "higher normalized total", "impact": "secured the award"}],
                    "why_focal_lost": [{"factor": "Narrative gaps",
                                        "evidence": "trailed the leader", "impact": "lower rank"}]}
        if "PROPOSAL EXCERPTS" in user_text:
            return {"rows": [{"category": "Price", "focal_points": ["fixed price"],
                              "winner_points": ["lower cost"]},
                             {"category": "Testing", "focal_points": ["SIT"],
                              "winner_points": ["parallel testing"]}]}
        raise AssertionError("unexpected prompt:\n" + user_text[:200])


def _make_project(root):
    def w(path, text):
        full = os.path.join(root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as fh:
            fh.write(text)

    w("projects/demo/rfp/rfp.txt",
      "SECTION II.D EVALUATION. Proposals will be evaluated on vendor qualifications, "
      "technical approach, and cost. Points will be allocated by weight. Minimum "
      "requirements must be met or the vendor is disqualified.")
    w("projects/demo/proposals/IBM/ibm.txt",
      "IBM technical approach and qualifications and cost narrative. " * 20)
    w("projects/demo/proposals/Deloitte/deloitte.txt",
      "Deloitte technical approach and qualifications and cost narrative. " * 20)
    w("projects/demo/scoresheet/sheet.txt",
      "COMPOSITE SCORES. Deloitte total 88. Vendor qualifications 5, technical approach 4.")
    return os.path.join(root, "projects", "demo")


def main():
    root = tempfile.mkdtemp(prefix="pipeline_mock_")
    proj_dir = _make_project(root)
    out_dir = os.path.join(root, "out")
    mock = MockBedrock()
    os.environ["SCORING_PPTX_ENABLED"] = "1"  # exercise the deck-content + render path

    out = run_scoring(
        "demo", focal_vendor="IBM",
        local_dir=proj_dir, bedrock=mock,
        out_dir=out_dir,
        template_path=None,
    )
    result = out.result

    # scheme parsed from RFP (mock)
    assert result.scheme.source == "parsed_from_rfp", result.scheme.source
    assert [d.id for d in result.scheme.dimensions] == [
        "vendor_qualifications", "technical_approach", "cost_price"]

    vendors = {v.vendor: v for v in result.vendors}
    assert set(vendors) == {"IBM", "Deloitte"}, set(vendors)

    # Deloitte: extracted cells (from scoresheet) + generated for the rest
    dl = vendors["Deloitte"]
    assert dl.cells["vendor_qualifications"].provenance == Provenance.EXTRACTED
    assert dl.cells["technical_approach"].provenance == Provenance.EXTRACTED
    assert dl.cells["cost_price"].provenance == Provenance.GENERATED  # not on the sheet
    # official total preserved
    assert dl.native_total == 88.0, dl.native_total

    # IBM: fully generated
    ibm = vendors["IBM"]
    assert all(c.provenance == Provenance.GENERATED for c in ibm.cells.values())

    # aggregation + CI
    assert result.ci is not None and result.ci.focal_vendor == "IBM"
    assert all(v.normalized_total_pct is not None for v in result.vendors)

    # artifacts
    assert os.path.exists(out.artifacts["json"])
    assert os.path.exists(out.artifacts["xlsx"])
    data = json.load(open(out.artifacts["json"]))
    assert data["schema_version"] == "1.0"
    provs = {c["provenance"] for v in data["vendors"] for c in v["cells"].values()}
    assert "extracted" in provs and "generated" in provs, provs

    # deck produced end-to-end (7 slides) via the wired deck-content path
    assert "pptx" in out.artifacts and os.path.exists(out.artifacts["pptx"]), out.artifacts
    from pptx import Presentation

    assert len(Presentation(out.artifacts["pptx"]).slides) == 7

    print("[PASS] pipeline mock integration")
    print("  vendors:", {k: (v.rank, round(v.normalized_total_pct, 1)) for k, v in vendors.items()})
    print("  CI:", result.ci.summary)
    print("  artifacts:", {k: os.path.basename(p) for k, p in out.artifacts.items()})
    print("  warnings:", result.warnings)


if __name__ == "__main__":
    main()
