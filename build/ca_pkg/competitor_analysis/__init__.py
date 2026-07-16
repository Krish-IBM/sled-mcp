"""SLED competitor bid-strategy analysis agent.

Backend service behind the MCP router's ``COMPETITOR_ANALYSIS_URL``. Draws on
the FOIA corpus (s3://competitive-intelligence-sled, organized
``<Vendor>/<Procurement>/...``) to profile a competitor's bid strategy across
five dimensions — solutioning, staffing, pricing, past performance, and win
themes — each ending with implications for IBM (the focal vendor).

Reuses the scoring agent's document ingestion (``scoring_agent.ingest``) and
Bedrock client (``scoring_agent.bedrock``); see competitor_analysis/README.md.
"""

# Load .env for local dev before any submodule reads os.environ. Runs first
# because importing any competitor_analysis.* module executes this __init__
# first. No-op in Lambda, where config comes from the function's env vars.
try:
    import env_config  # noqa: F401  (project-root loader; absent in deploy pkg)
except Exception:
    pass

__version__ = "0.1.0"
