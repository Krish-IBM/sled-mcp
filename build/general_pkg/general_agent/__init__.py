"""SLED general-purpose ("catch-all") assistant.

Backend service behind the MCP router's ``GENERAL_AGENT_URL``. Answers open-ended
questions the specialized agents don't cover: it first tries the SLED competitive-
intelligence corpus, and otherwise (or in addition) answers from the model's
general knowledge. Synchronous — a single Bedrock generation call, so no async
job model.

See general_agent/README.md for the full design.
"""

# Load .env for local dev before any submodule reads os.environ. Runs first
# because importing any general_agent.* module executes this __init__ first.
# No-op in Lambda, where config comes from the function's environment variables.
try:
    import env_config  # noqa: F401  (project-root loader; absent in deploy pkg)
except Exception:
    pass

__version__ = "0.1.0"
