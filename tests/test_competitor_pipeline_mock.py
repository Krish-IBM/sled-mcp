"""Integration test of the competitor-analysis pipeline with MOCK S3 + Bedrock.

Exercises resolve -> enumerate -> extract -> digest -> synthesize -> render
without any AWS calls, using tiny text documents in a fake CI bucket.

Run:  ./.venv/bin/python tests/test_competitor_pipeline_mock.py
"""

from __future__ import annotations

import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from competitor_analysis.models import DIMENSIONS
from competitor_analysis.pipeline import AnalysisError, run_analysis


class FakeS3:
    """Just enough of the S3 client surface for the pipeline."""

    def __init__(self, objects):
        self.objects = objects            # key -> bytes (the CI bucket)
        self.uploaded = {}                # key -> bytes (the output bucket)

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        outer = self

        class P:
            def paginate(self, Bucket, Prefix="", Delimiter=None):
                keys = sorted(k for k in outer.objects if k.startswith(Prefix))
                if Delimiter:
                    tops = sorted({k.split(Delimiter)[0] + Delimiter
                                   for k in keys if Delimiter in k})
                    yield {"CommonPrefixes": [{"Prefix": t} for t in tops]}
                else:
                    yield {"Contents": [
                        {"Key": k, "Size": len(outer.objects[k])} for k in keys]}

        return P()

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key])}

    def upload_file(self, local, bucket, key):
        with open(local, "rb") as fh:
            self.uploaded[key] = fh.read()


class MockBedrock:
    def converse(self, user_text, **kwargs):   # OCR path — unused for .txt docs
        return ""

    def converse_json(self, user_text, **kwargs):
        if "DOCUMENT TEXT:" in user_text:      # per-procurement digest
            assert "Acme Corp" in user_text
            return {
                "client": "State X", "year": "2020", "outcome": "won (award letter)",
                "dimension_notes": {
                    "pricing": "Bid $10M fixed price, 20% below field.",
                    "staffing": "60% offshore mix.",
                    "not_a_dimension": "must be dropped",
                },
            }
        if "Evidence digests" in user_text:    # synthesis
            dims = {key: {
                "analysis": f"Pattern for {key}.",
                "evidence": [{"procurement": "State X ERP (2020)", "detail": "Fact."}],
                "ibm_implications": f"IBM counter for {key}.",
            } for key in DIMENSIONS}
            return {"executive_summary": "Acme is a price shark.", "dimensions": dims}
        raise AssertionError("unexpected prompt:\n" + user_text[:200])


def _fake_bucket():
    doc = ("Acme Corp proposal for the State X ERP procurement. Fixed price $10M. "
           "Offshore delivery center staffing. " * 30).encode()
    return {
        "Acme Corp/State X ERP (2020)/Technical Proposal.txt": doc,
        "Acme Corp/State X ERP (2020)/Pricing Workbook.txt": doc,
        "Acme Corp/State Y CCWIS (2022)/Acme Response.txt": doc,
        "Acme Corp/press-photo.png": b"not-readable",          # skipped: extension
        "Other Vendor/doc.txt": doc,                           # different vendor
        "00_FOIA Analysis/notes.txt": doc,                     # excluded working folder
    }


def main():
    s3 = FakeS3(_fake_bucket())
    out = run_analysis(
        "acme",                       # fuzzy: resolves to "Acme Corp"
        focal="IBM",
        ci_bucket="ci-bucket",
        out_bucket="out-bucket",
        out_prefix="competitor-analysis/outputs/Acme_Corp/job1/",
        s3=s3,
        bedrock=MockBedrock(),
    )
    a = out.analysis
    assert a.competitor == "Acme Corp", a.competitor
    assert len(a.procurement_digests) == 2, [d.procurement for d in a.procurement_digests]
    procs = {d.procurement for d in a.procurement_digests}
    assert procs == {"State X ERP (2020)", "State Y CCWIS (2022)"}, procs
    # invalid dimension keys from the model are dropped
    assert all(set(d.dimension_notes) <= set(DIMENSIONS) for d in a.procurement_digests)
    assert a.docs_analyzed == 3, a.docs_analyzed
    assert a.docs_skipped >= 1  # the .png
    assert a.executive_summary == "Acme is a price shark."
    assert [d.key for d in a.dimensions] == list(DIMENSIONS)
    assert all(d.ibm_implications for d in a.dimensions)

    # artifacts uploaded to the output bucket
    assert set(out.artifacts) == {"json", "docx", "pptx"}, out.artifacts
    data = json.loads(s3.uploaded[out.artifacts["json"]])
    assert data["competitor"] == "Acme Corp"
    assert s3.uploaded[out.artifacts["docx"]][:2] == b"PK"  # zip magic = valid docx
    assert s3.uploaded[out.artifacts["pptx"]][:2] == b"PK"  # zip magic = valid pptx

    # ambiguity / miss paths
    try:
        run_analysis("nonexistent vendor xyz", focal="IBM", ci_bucket="ci-bucket",
                     out_bucket="out-bucket", out_prefix="p/", s3=s3, bedrock=MockBedrock())
        raise AssertionError("expected AnalysisError")
    except AnalysisError as exc:
        assert "No competitor folder matches" in str(exc)

    print("[PASS] competitor pipeline mock integration")
    print("  procurements:", sorted(procs))
    print("  artifacts:", sorted(out.artifacts))
    print("  warnings:", a.warnings)


if __name__ == "__main__":
    main()
