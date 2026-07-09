"""Base-rubric loading.

The ``base_rubric.yaml`` sibling file is the standard SLED rubric. It loads into
a :class:`~scoring_agent.models.SchemeSpec` that either serves as the default
scheme or as the starting point ``rfp_scheme.py`` adapts per solicitation.
"""

from __future__ import annotations

import os
from typing import Optional

import yaml

from ..models import SchemeSpec

_RUBRIC_PATH = os.path.join(os.path.dirname(__file__), "base_rubric.yaml")


def load_base_rubric(project_id: str = "", path: Optional[str] = None) -> SchemeSpec:
    """Load the standard base rubric as a SchemeSpec.

    ``project_id`` is stamped onto the returned spec (the yaml omits it since
    the rubric is project-agnostic).
    """
    with open(path or _RUBRIC_PATH, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    data = dict(data)
    data["project_id"] = project_id
    return SchemeSpec.from_dict(data)
