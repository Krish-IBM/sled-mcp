"""Amazon Bedrock (Claude) client wrapper.

Thin layer over the Bedrock Runtime ``Converse`` API used by every LLM step:
RFP-scheme parsing, scoresheet extraction, and proposal scoring. Supports
document blocks (Claude reads PDFs — including *scanned* PDFs — via its native
document/vision support) and image blocks, plus a robust JSON extractor and
throttling backoff.

Model IDs are env-configured (they differ per Bedrock account/region and change
over time). Set the recommended latest Claude inference-profile IDs:

    SCORING_MODEL_ID        strong model (scheme parse, scoring)   e.g. an Opus/Sonnet profile
    SCORING_FAST_MODEL_ID   fast model (OCR/ingest, light tasks)   e.g. a Sonnet/Haiku profile
    BEDROCK_REGION          defaults to AWS_REGION or us-east-1

``bedrock.py`` has no import-time AWS dependency beyond boto3, so modules that
import it stay unit-testable; the client is created lazily on first call.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# Recommended: override with the latest Claude inference-profile IDs enabled in
# your Bedrock account. These conservative defaults are broadly available.
_DEFAULT_STRONG = "anthropic.claude-3-5-sonnet-20241022-v2:0"
_DEFAULT_FAST = "anthropic.claude-3-5-haiku-20241022-v1:0"

# Bedrock Converse document formats we may hand over.
_PDF = "pdf"
_DOC_NAME_RE = re.compile(r"[^A-Za-z0-9 \-\(\)\[\]]+")

_RETRYABLE = {"ThrottlingException", "ModelTimeoutException", "ServiceUnavailableException",
              "InternalServerException", "ModelNotReadyException"}


def _sanitize_doc_name(name: str) -> str:
    cleaned = _DOC_NAME_RE.sub(" ", name).strip() or "document"
    return cleaned[:60]


@dataclass
class DocBlock:
    """A document to attach to a Converse message."""

    name: str
    data: bytes
    fmt: str = _PDF


@dataclass
class ImageBlock:
    data: bytes
    fmt: str = "png"


@dataclass
class BedrockClient:
    """Lazily-initialized Bedrock Runtime wrapper."""

    region: Optional[str] = None
    strong_model: str = field(default_factory=lambda: os.environ.get("SCORING_MODEL_ID", _DEFAULT_STRONG))
    fast_model: str = field(default_factory=lambda: os.environ.get("SCORING_FAST_MODEL_ID", _DEFAULT_FAST))
    max_retries: int = field(default_factory=lambda: int(os.environ.get("BEDROCK_MAX_RETRIES", "3")))
    _client: Any = field(default=None, repr=False)

    def _runtime(self):
        if self._client is None:
            import boto3  # imported lazily so tests can mock/avoid AWS
            from botocore.config import Config

            region = self.region or os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_REGION") or "us-east-1"
            # Bound each call so one hung/throttled request can't consume the
            # worker's whole Lambda budget. Our own backoff (_invoke_with_retry)
            # sits on top, so keep botocore's internal retries minimal.
            cfg = Config(
                region_name=region,
                connect_timeout=int(os.environ.get("BEDROCK_CONNECT_TIMEOUT", "10")),
                read_timeout=int(os.environ.get("BEDROCK_READ_TIMEOUT", "60")),
                retries={"max_attempts": 2, "mode": "standard"},
            )
            self._client = boto3.client("bedrock-runtime", region_name=region, config=cfg)
        return self._client

    # -- core call -------------------------------------------------------- #
    def converse(
        self,
        user_text: str,
        *,
        system: Optional[str] = None,
        documents: Optional[List[DocBlock]] = None,
        images: Optional[List[ImageBlock]] = None,
        model: Optional[str] = None,
        max_tokens: int = 4000,
        temperature: Optional[float] = None,
        fast: bool = False,
    ) -> str:
        """Single-turn Converse call; returns concatenated text output.

        ``temperature`` is omitted unless explicitly set — the latest Claude
        models on Bedrock reject a ``temperature`` field in inferenceConfig.
        """
        content: List[Dict[str, Any]] = []
        seen: set = set()
        for i, d in enumerate(documents or []):
            name = _sanitize_doc_name(d.name)
            while name.lower() in seen:  # names must be unique
                name = f"{name} {i}"
            seen.add(name.lower())
            content.append({"document": {"name": name, "format": d.fmt, "source": {"bytes": d.data}}})
        for img in images or []:
            content.append({"image": {"format": img.fmt, "source": {"bytes": img.data}}})
        content.append({"text": user_text})

        inference: Dict[str, Any] = {"maxTokens": max_tokens}
        if temperature is not None:
            inference["temperature"] = temperature
        kwargs: Dict[str, Any] = {
            "modelId": model or (self.fast_model if fast else self.strong_model),
            "messages": [{"role": "user", "content": content}],
            "inferenceConfig": inference,
        }
        if system:
            kwargs["system"] = [{"text": system}]

        resp = self._invoke_with_retry(kwargs)
        blocks = resp.get("output", {}).get("message", {}).get("content", [])
        return "".join(b.get("text", "") for b in blocks)

    def _invoke_with_retry(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        from botocore.exceptions import ClientError

        delay = 1.0
        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                return self._runtime().converse(**kwargs)
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                last_exc = e
                if code not in _RETRYABLE or attempt == self.max_retries - 1:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 20.0)
        if last_exc:
            raise last_exc
        raise RuntimeError("bedrock converse failed without exception")

    # -- JSON convenience ------------------------------------------------- #
    def converse_json(
        self,
        user_text: str,
        *,
        system: Optional[str] = None,
        documents: Optional[List[DocBlock]] = None,
        images: Optional[List[ImageBlock]] = None,
        model: Optional[str] = None,
        max_tokens: int = 4000,
        fast: bool = False,
        retries_on_parse: int = 1,
    ) -> Any:
        """Call the model and parse a JSON object/array from its reply.

        Reinforces JSON-only output and retries once on a parse failure.
        """
        sys_json = (system or "") + (
            "\n\nRespond with a single valid JSON value and nothing else — "
            "no prose, no markdown fences."
        )
        prompt = user_text
        for attempt in range(retries_on_parse + 1):
            text = self.converse(
                prompt, system=sys_json, documents=documents, images=images,
                model=model, max_tokens=max_tokens, fast=fast,
            )
            parsed = extract_json(text)
            if parsed is not None:
                return parsed
            prompt = (
                user_text
                + "\n\nYour previous reply was not valid JSON. Return ONLY the JSON value."
            )
        raise ValueError("model did not return parseable JSON")


# --------------------------------------------------------------------------- #
# JSON extraction (module-level so it is trivially unit-testable)
# --------------------------------------------------------------------------- #
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> Optional[Any]:
    """Best-effort parse of a JSON object/array embedded in model output."""
    if not text:
        return None
    text = text.strip()
    # 1. direct
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 2. fenced ```json ... ```
    m = _FENCE_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            text = m.group(1).strip()
    # 3. first balanced { } or [ ] span
    for opener, closer in (("{", "}"), ("[", "]")):
        span = _balanced_span(text, opener, closer)
        if span:
            try:
                return json.loads(span)
            except json.JSONDecodeError:
                continue
    return None


def _balanced_span(text: str, opener: str, closer: str) -> Optional[str]:
    start = text.find(opener)
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


# Module-level default client (created lazily on first use by callers).
_default: Optional[BedrockClient] = None


def default_client() -> BedrockClient:
    global _default
    if _default is None:
        _default = BedrockClient()
    return _default
