"""JSON artifact renderer."""

from __future__ import annotations

import json

from .models import CompetitorAnalysis


def render_json(analysis: CompetitorAnalysis, path: str) -> str:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(analysis.to_dict(), fh, indent=2, ensure_ascii=False)
    return path
