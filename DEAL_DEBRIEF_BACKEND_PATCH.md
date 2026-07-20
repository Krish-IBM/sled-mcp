# deal-analysis-agent (Deal Debrief) — backend patch to fold into source

The `sled-deal-analysis-agent-py` Lambda was wired into the MCP router on 2026-07-20.
Two fixes were applied **directly to the deployed zip** (via `update-function-code`) to make
it work through Otto. Its source-of-truth lives in your separate deal-analysis-agent project —
**apply these same two edits there**, or the next redeploy from source will regress them.

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

## Router wiring (this repo, already done)
- `lambda_handler.py`: added `DEAL_DEBRIEF_URL` env read + `deal_debrief` entry in `AGENT_REGISTRY`
  (`payload_key="file_query"`, tool `sled_deal_debrief`, env-gated).
- Front door: HTTP API `saziljapre` → `DEAL_DEBRIEF_URL=https://saziljapre.execute-api.us-east-1.amazonaws.com/`.
- Router env merged 13→14 keys; redeployed code-only to **v7** (rollback: `build/mcp_server.zip.predeploy-deal-20260720`).
- Deal Lambda rollback zip: kept in scratchpad as `deal.zip.orig` (re-upload via `update-function-code` to revert).
