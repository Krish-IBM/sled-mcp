"""SLED competitive bid-scoring agent.

Backend service behind the MCP router's ``SCORING_AGENT_URL``. Produces a
side-by-side scorecard (evaluation dimensions x vendors + notes) for competing
bids on a government procurement, through a competitive-intelligence lens.

See scoring_agent/README.md and the approved plan for the full design.
"""

# Load .env for local dev before any submodule reads os.environ. Runs first
# because importing any scoring_agent.* module executes this __init__ first.
# No-op in Lambda, where config comes from the function's environment variables.
try:
    import env_config  # noqa: F401  (project-root loader; absent in deploy pkg)
except Exception:
    pass

__version__ = "0.1.0"
