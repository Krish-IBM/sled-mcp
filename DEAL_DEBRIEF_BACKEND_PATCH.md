# deal-analysis-agent (Deal Debrief) — backend patch to fold into source

The `sled-deal-analysis-agent-py` Lambda was wired into the MCP router on 2026-07-20.
Fixes were applied **directly to the deployed zip** (via `update-function-code`) to make
it work through Otto. Its source-of-truth lives in your separate deal-analysis-agent project —
**apply these edits there**, or the next redeploy from source will regress them.

> ⚠️ **REGRESSED ONCE ALREADY.** A source redeploy on **2026-07-21 14:21 UTC** wiped Patch 2
> (`maxTokens` was back to 1024, `_loads_lenient` gone) → the live 500 `"AI inference returned no
> data"` came right back through Otto. Re-applied the same day and added **Fix 3** (fuzzy match +
> read guard). If you redeploy this Lambda from source, confirm ALL FOUR fixes below survive.
> Rollback of the pre-fix zip: `build/sled-deal-analysis-agent-py.zip.rollback-20260721-150112`.
>
> **Fix 4 (2026-07-21, latency)** dropped the redundant second Bedrock pass to pull the
> synchronous request off the API-Gateway/router timeout cliff. Deployed as Lambda **v11**.
> Rollback of the pre-Fix-4 (v10, all of 1–3) zip: `build/sled-deal-analysis-agent-py.zip.rollback-droppass2-20260721`.

## Why
1. **`handler.py`** read `event.get("file_query")` off the raw event root. Behind the HTTP API
   front door (proxy integration), the payload arrives as a JSON string in `event["body"]`, so
   `file_query` was always empty → `400 Missing required field`. The patch parses `event["body"]`
   (matching the scoring/competitor handlers) while still accepting a direct invoke.
2. **`tools.py` `run_ai_inference`** capped `maxTokens: 1024`. The full scorecard JSON exceeds
   that, so the model output was truncated → `json.loads` failed → the function returned `None`
   → `500 "AI inference returned no data"`. Bumped to `8192` and added a lenient JSON parser
   (`_loads_lenient`) that strips ```json fences / prose. `generate_score_rationales` bumped
   512→1024 for the same reason (it already fails soft).

## handler.py
```diff
 def lambda_handler(event, context):
-    file_query = event.get("file_query", "").strip()
+    # Accept both a direct invoke ({"file_query": ...}) and an API Gateway / HTTP
+    # API proxy event (the JSON payload arrives as a string under event["body"]).
+    payload = event if isinstance(event, dict) else {}
+    body = payload.get("body")
+    if isinstance(body, str):
+        try:
+            payload = json.loads(body or "{}")
+        except json.JSONDecodeError:
+            payload = {}
+    elif isinstance(body, dict):
+        payload = body
+
+    file_query = (payload.get("file_query") or "").strip()
     if not file_query:
```

## tools.py
```diff
+def _loads_lenient(text: str):
+    """Parse model output as JSON, tolerating ```json fences / stray prose by
+    falling back to the outermost {...} span. Raises json.JSONDecodeError if no
+    valid JSON object can be recovered."""
+    try:
+        return json.loads(text)
+    except json.JSONDecodeError:
+        pass
+    stripped = text.strip()
+    if stripped.startswith("```"):
+        stripped = stripped.split("```", 2)[1] if stripped.count("```") >= 2 else stripped
+        if stripped.lstrip().lower().startswith("json"):
+            stripped = stripped.lstrip()[4:]
+        try:
+            return json.loads(stripped.strip())
+        except json.JSONDecodeError:
+            pass
+    start, end = text.find("{"), text.rfind("}")
+    if start != -1 and end > start:
+        return json.loads(text[start:end + 1])
+    return json.loads(text)  # re-raise the original-style error
+
+
 def run_ai_inference(prompt, model_id="us.anthropic.claude-sonnet-4-20250514-v1:0"):
     ...
             inferenceConfig={
-                "maxTokens": 1024,
+                "maxTokens": 8192,
                 "temperature": 0.3,
                 "topP": 0.9,
             },
         )
         output_text = response["output"]["message"]["content"][0]["text"]
-        output_json = json.loads(output_text)
+        output_json = _loads_lenient(output_text)
         return output_json

 def generate_score_rationales(...):
     ...
-            inferenceConfig={"maxTokens": 512, "temperature": 0.2, "topP": 0.9},
+            inferenceConfig={"maxTokens": 1024, "temperature": 0.2, "topP": 0.9},
         )
         raw = response["output"]["message"]["content"][0]["text"]
-        return json.loads(raw)
+        return _loads_lenient(raw)
```

## Fix 3 — fuzzy match hardening + read guard (added 2026-07-21)
Symptom: `file_query="Loudoun County payroll"` → fast **2.5s generic 500** (`{"message":"Internal
Server Error"}`). Cause: the real file is `...ERP HCM **Payoll** Implementation.docx` (typo — no "r"),
so `find_s3_file`'s "all tokens present" rule missed and it fell to "any token, first hit wins",
which returned a **non-.docx** object; `read_docx_from_s3` then threw uncaught → raw 500.

`tools.py find_s3_file`: (1) only collect keys ending `.docx` (handler can't parse anything else);
(2) replace "first hit" with a **best token-overlap score** (`max` over `(score, -len(key))`), raising
`FileNotFoundError` only when the best score is 0. `handler.py`: wrap `read_docx_from_s3` in
try/except → return a clean **502** `"Matched file could not be read as a .docx"` instead of a raw 500.

## Fix 4 — drop the redundant second Bedrock pass (added 2026-07-21, latency)
Symptom: the whole request ran **~28s** end-to-end, hard against the router's **29s** urllib
timeout and the API Gateway **30s** hard cap → intermittent timeout/500 through Otto.

Measured breakdown (live Loudoun deal): Pass 1 (`run_ai_inference`, extract scorecard JSON) **~20s**;
Pass 2 (`generate_score_rationales`) **~5.4s**; S3 read + PPTX build + upload **~3s**. Pass 1 output
is bounded by the fixed 6-dimension schema, so its latency does **not** grow with transcript length —
the ~28s is roughly steady-state, not a "long transcript" edge case.

Pass 2 was **redundant**: the extraction schema already gives every scorecard dimension its own
`summary`, and `format_summary_text` already falls back to that summary when rationales are absent
(`evidence = rationale if rationale else summary`). The PPTX never used rationales at all
(`create_ibm_themed_ppt(extracted_data)` only). So Fix 4 removes the pass-2 call in `handler.py`:

```diff
-    # 4. Generate score rationales (second pass — evidence citations, doesn't touch JSON)
-    rationales = tools.generate_score_rationales(
-        document_text, extracted_data.get("scorecard", {})
-    )
-
-    # 5. Build the PowerPoint → bytes (uses original JSON only, rationales not injected)
-    pptx_bytes = tools.create_ibm_themed_ppt(extracted_data)
+    # 4. Build the PowerPoint → bytes (uses the extracted JSON only)
+    pptx_bytes = tools.create_ibm_themed_ppt(extracted_data)
...
-    summary = tools.format_summary_text(extracted_data, rationales=rationales)
+    summary = tools.format_summary_text(extracted_data)
```

`tools.generate_score_rationales` is now unused (kept in `tools.py` as harmless dead code; delete it
in source if you prefer). Result: end-to-end median **~28s → ~19s** (measured 6 live runs: 16.5,
16.9, 19.4, 21.0, 26.7s + one 30.1s outlier under a rapid-fire test burst). **5/6 succeeded.**

## Residual latency caveat (not fully eliminated by Fix 4)
Pass 1's own Bedrock generation latency is variable and its **tail can still reach/exceed 30s**
(one of six live runs 503'd at the 30s API-GW cap, aggravated by back-to-back test bursts →
Bedrock throttling; realistic single-request usage lands ~16–27s). Fix 4 pulls the *typical* case
well under the ceiling but does not guarantee zero timeouts. The **durable** elimination is an async
job model (like scoring/competitor): return a `job_id` immediately, do the Bedrock/PPTX work in a
background invoke, and poll via `status`/`result`. That spans backend + router wiring and was
deferred. Note: raising the router urllib timeout won't help — the API Gateway 30s is a hard max.

## Router wiring (this repo, already done)
- `lambda_handler.py`: added `DEAL_DEBRIEF_URL` env read + `deal_debrief` entry in `AGENT_REGISTRY`
  (`payload_key="file_query"`, tool `sled_deal_debrief`, env-gated).
- Front door: HTTP API `saziljapre` → `DEAL_DEBRIEF_URL=https://saziljapre.execute-api.us-east-1.amazonaws.com/`.
- Router env merged 13→14 keys; redeployed code-only to **v7** (rollback: `build/mcp_server.zip.predeploy-deal-20260720`).
- Deal Lambda rollback zip: kept in scratchpad as `deal.zip.orig` (re-upload via `update-function-code` to revert).
