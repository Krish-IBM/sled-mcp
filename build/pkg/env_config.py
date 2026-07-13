"""Central .env loader for LOCAL development.

Import this once, as early as possible, before any ``os.environ`` reads::

    try:
        import env_config  # noqa: F401  # loads .env for local dev
    except Exception:
        pass

Why the try/except: in AWS Lambda, configuration comes from the function's own
environment variables (set at deploy time by deploy.sh / deploy_scoring.sh), and
`.env` is *not* bundled into the deploy package. Importing this there is a
harmless no-op — there is no `.env` to find, and even if there were,
``override=False`` / ``setdefault`` mean a real environment variable always wins.

python-dotenv is used if installed (``pip install -r requirements-dev.txt``);
otherwise a tiny built-in parser handles ``KEY=VALUE`` lines so no dependency is
required for `.env` to work locally.
"""

from __future__ import annotations

import os
from pathlib import Path


def _find_dotenv() -> Path | None:
    """Return the nearest `.env`, searching upward from the CWD and from here."""
    seen: set[Path] = set()
    for start in (Path.cwd(), Path(__file__).resolve().parent):
        p = start
        while p not in seen:
            seen.add(p)
            candidate = p / ".env"
            if candidate.is_file():
                return candidate
            if p.parent == p:  # filesystem root
                break
            p = p.parent
    return None


def _parse_line(raw: str) -> tuple[str, str] | None:
    """Parse one `KEY=VALUE` line (dotenv-style), or None to skip it."""
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    if line.startswith("export "):
        line = line[len("export "):]
    key, _, val = line.partition("=")
    key = key.strip()
    val = val.strip()
    if val[:1] in ("'", '"'):  # quoted value: take up to the closing quote
        quote = val[0]
        end = val.find(quote, 1)
        val = val[1:end] if end != -1 else val[1:]
    else:  # unquoted: strip an inline " # comment"
        hash_at = val.find(" #")
        if hash_at != -1:
            val = val[:hash_at]
        val = val.strip()
    return key, val


def load() -> bool:
    """Load the nearest `.env` into os.environ without overriding existing vars."""
    path = _find_dotenv()
    if path is None:
        return False
    try:
        from dotenv import load_dotenv  # optional, nicer parsing
        load_dotenv(path, override=False)
        return True
    except ImportError:
        for raw in path.read_text().splitlines():
            parsed = _parse_line(raw)
            if parsed:
                os.environ.setdefault(*parsed)  # real env vars win
        return True


_LOADED = load()
