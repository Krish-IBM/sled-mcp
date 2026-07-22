"""Offline unit tests for the general ("catch-all") agent (no AWS calls).

Mocks the corpus retrieve + Bedrock converse so every branch is exercised without
touching AWS. Run:  ./.venv/bin/python tests/test_general_offline.py
(also works under pytest).
"""

from __future__ import annotations

import json
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from general_agent import handler, retrieve


# ── body / bearer parsing ─────────────────────────────────────────────────────

def test_extract_query():
    # raw invoke (dict at root)
    assert handler._extract_query({"query": "hello"}) == "hello"
    # HTTP-API proxy: payload is a JSON string in event["body"]
    assert handler._extract_query({"body": json.dumps({"query": "hi there"})}) == "hi there"
    # 'question' alias also accepted
    assert handler._extract_query({"body": json.dumps({"question": "q?"})}) == "q?"
    # missing / malformed -> empty
    assert handler._extract_query({"body": "not json"}) == ""
    assert handler._extract_query({}) == ""
    print("[PASS] extract_query")


def test_extract_bearer():
    ev = {"headers": {"Authorization": "Bearer abc.def"}}
    assert handler._extract_bearer(ev) == "abc.def"
    ev2 = {"headers": {"authorization": "raw-token"}}  # case-insensitive, no prefix
    assert handler._extract_bearer(ev2) == "raw-token"
    assert handler._extract_bearer({}) == ""
    print("[PASS] extract_bearer")


# ── corpus gathering (retrieve vs docs fallback) ──────────────────────────────

def test_gather_corpus_retrieve_hit():
    hi = [{"text": "Deloitte proposed X for $5M", "score": 0.72, "source": "s3://b/Deloitte/prop.pdf"}]
    with mock.patch.object(retrieve, "retrieve", return_value=hi):
        ctx, source = retrieve.gather_corpus_context("q", min_score=0.4)
    assert source == "retrieve"
    assert ctx and "Deloitte proposed X" in ctx and "prop.pdf" in ctx
    print("[PASS] gather corpus: retrieve hit")


def test_gather_corpus_retrieve_below_threshold():
    lo = [{"text": "loosely related", "score": 0.12, "source": "s3://b/x.pdf"}]
    with mock.patch.object(retrieve, "retrieve", return_value=lo):
        ctx, source = retrieve.gather_corpus_context("q", min_score=0.4)
    assert ctx is None and source is None  # nothing on-topic -> answer generally
    print("[PASS] gather corpus: below threshold -> none")


def test_gather_corpus_access_denied_falls_back_to_docs():
    def boom(*a, **k):
        raise Exception("AccessDeniedException: not authorized to perform bedrock:Retrieve")

    with mock.patch.object(retrieve, "retrieve", side_effect=boom), \
         mock.patch.object(retrieve, "docs_fallback", return_value="corpus says: SC RFP 5400011045") as df:
        ctx, source = retrieve.gather_corpus_context("q", docs_url="https://docs", bearer="tok")
    assert source == "docs"
    assert ctx == "corpus says: SC RFP 5400011045"
    df.assert_called_once()  # the docs endpoint was consulted
    print("[PASS] gather corpus: AccessDenied -> docs fallback")


def test_docs_fallback_unwraps_proxy_envelope():
    # The live docs endpoint returns its Lambda's raw proxy dict, so the real
    # {"response":..} is nested in outer["body"] (a JSON string). Must unwrap.
    inner = json.dumps({"response": "Deloitte proposed GovConnect", "engine": "retrieve_and_generate"})
    outer = json.dumps({"statusCode": 200, "headers": {}, "body": inner})

    class _Resp:
        def read(self):
            return outer.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with mock.patch("urllib.request.urlopen", return_value=_Resp()):
        ans = retrieve.docs_fallback("q", docs_url="https://docs", bearer="t")
    assert ans == "Deloitte proposed GovConnect"

    # flat shape (proxy already unwrapped) must still work
    flat = json.dumps({"response": "flat answer"})

    class _Resp2(_Resp):
        def read(self):
            return flat.encode("utf-8")

    with mock.patch("urllib.request.urlopen", return_value=_Resp2()):
        assert retrieve.docs_fallback("q", docs_url="https://docs") == "flat answer"
    print("[PASS] docs_fallback unwraps proxy envelope")


def test_gather_corpus_all_fail():
    with mock.patch.object(retrieve, "retrieve", side_effect=Exception("boom")), \
         mock.patch.object(retrieve, "docs_fallback", return_value=None):
        ctx, source = retrieve.gather_corpus_context("q", docs_url="https://docs")
    assert ctx is None and source is None
    print("[PASS] gather corpus: both paths fail -> none")


# ── handler end-to-end (corpus branch, general branch, errors) ────────────────

class _FakeBedrock:
    def __init__(self):
        self.calls = []

    def converse(self, user_text, *, system=None, max_tokens=None, **kw):
        self.calls.append({"user_text": user_text, "system": system})
        return "ANSWER"


def test_handler_corpus_branch():
    fake = _FakeBedrock()
    with mock.patch.object(handler, "gather_corpus_context", return_value=("CTX-CHUNKS", "retrieve")), \
         mock.patch.object(handler, "_bedrock", return_value=fake):
        resp = handler.lambda_handler({"query": "who bid on FL CCWIS 2022?"}, None)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["response"] == "ANSWER"
    assert body["engine"] == "corpus" and body["corpus_source"] == "retrieve"
    # the corpus context was actually handed to the model
    assert "CTX-CHUNKS" in fake.calls[0]["user_text"]
    print("[PASS] handler: corpus branch")


def test_handler_no_corpus_branch():
    # No corpus material -> the model is told to say the corpus lacks it, NOT to
    # answer from general knowledge. engine is "none".
    fake = _FakeBedrock()
    with mock.patch.object(handler, "gather_corpus_context", return_value=(None, None)), \
         mock.patch.object(handler, "_bedrock", return_value=fake):
        resp = handler.lambda_handler({"body": json.dumps({"query": "explain FedRAMP levels"})}, None)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["engine"] == "none" and body["corpus_source"] == ""
    # prompt tells the model there was no corpus material and to NOT use general knowledge
    user_text = fake.calls[0]["user_text"]
    assert "No SLED corpus material was found" in user_text
    assert "Do NOT" in user_text and "general knowledge" in user_text
    # the corpus-only rule is enforced in the system prompt too
    assert "STRICTLY" in fake.calls[0]["system"]
    print("[PASS] handler: no-corpus branch stays corpus-only")


def test_handler_corpus_failure_is_soft():
    # if gathering corpus itself throws, we still answer (no-corpus path), never 500 on it
    fake = _FakeBedrock()
    with mock.patch.object(handler, "gather_corpus_context", side_effect=Exception("network")), \
         mock.patch.object(handler, "_bedrock", return_value=fake):
        resp = handler.lambda_handler({"query": "q"}, None)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["engine"] == "none"
    print("[PASS] handler: corpus gather failure is soft")


def test_handler_empty_query():
    resp = handler.lambda_handler({"query": "   "}, None)
    assert resp["statusCode"] == 400
    print("[PASS] handler: empty query -> 400")


def test_handler_generation_failure():
    class _Boom:
        def converse(self, *a, **k):
            raise Exception("ThrottlingException")

    with mock.patch.object(handler, "gather_corpus_context", return_value=(None, None)), \
         mock.patch.object(handler, "_bedrock", return_value=_Boom()):
        resp = handler.lambda_handler({"query": "q"}, None)
    assert resp["statusCode"] == 500
    print("[PASS] handler: generation failure -> 500")


if __name__ == "__main__":
    test_extract_query()
    test_extract_bearer()
    test_gather_corpus_retrieve_hit()
    test_gather_corpus_retrieve_below_threshold()
    test_gather_corpus_access_denied_falls_back_to_docs()
    test_docs_fallback_unwraps_proxy_envelope()
    test_gather_corpus_all_fail()
    test_handler_corpus_branch()
    test_handler_no_corpus_branch()
    test_handler_corpus_failure_is_soft()
    test_handler_empty_query()
    test_handler_generation_failure()
    print("ALL PASS")
