"""Canonical JSON output.

The JSON is the source of truth: full scheme, every cell's score / provenance /
evidence / rationale, totals, ranking, and CI insights. The Excel and PPTX
renderers are views over the same :class:`ScorecardResult`.
"""

from __future__ import annotations

import json
from typing import Optional

from .models import ScorecardResult


def render_json(result: ScorecardResult, path: Optional[str] = None, indent: int = 2) -> str:
    text = json.dumps(result.to_dict(), indent=indent, ensure_ascii=False)
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    return text
