import boto3
import json

# Knowledge base backing the SLED competitive-intelligence corpus
# (data source = s3://competitive-intelligence-sled).
KB_ID = "FIFNL0U11I"

# Modern generation model (confirmed available in this account). Used for
# retrieve_and_generate. The old Bedrock Agent ran Claude 3 Sonnet, which
# hard-refused ("Sorry, I am unable to assist...") even when the KB returned
# relevant passages. Sonnet 4 synthesizes the retrieved passages instead.
MODEL_ARN = (
    "arn:aws:bedrock:us-east-1:211125468742:"
    "inference-profile/us.anthropic.claude-sonnet-4-20250514-v1:0"
)
NUM_RESULTS = 10

# Fallback: the original Bedrock Agent (kept so a permissions gap on the
# Lambda role degrades gracefully to the previous behavior, never worse).
AGENT_ID = "WFJKWV1RKB"
AGENT_ALIAS_ID = "SNMYYM5VOU"

PROMPT_TEMPLATE = """You are a competitive intelligence assistant for IBM's SLED (State, Local, and Education) business. You are given search results from a knowledge base of real competitor and procurement documents: vendor proposals, cost/pricing workbooks, RFPs and requirements, and evaluation scoresheets for vendors such as IBM, Accenture, Deloitte, CGI, Cognizant, Capgemini, and others.

Answer the user's question using only the search results below.

Guidelines:
- Synthesize across the search results and give a direct, useful answer. Prefer specifics (vendor names, procurement names, dollar figures, scores) when they appear.
- Cite the source document and the vendor or procurement it comes from.
- If the search results do not fully answer the question, do NOT refuse. Briefly summarize the most relevant information that IS present and name the procurements or vendors it covers, so the user understands what the corpus contains.
- The corpus is organized by vendor and by procurement and holds proposals, pricing, and scoresheets rather than a single "deals won" summary. For broad questions, summarize the most relevant documents you find.
- Never fabricate documents, vendors, scores, or contract values. Report only what the search results contain, and say plainly when they are silent.

Search results:
$search_results$

$output_format_instructions$"""


def _extract_query(event):
    body = event
    if isinstance(event, dict) and "body" in event:
        try:
            body = json.loads(event["body"])
        except (json.JSONDecodeError, TypeError):
            body = event.get("body", {}) or {}
    if not isinstance(body, dict):
        body = {}
    return body.get("query", "")


def _retrieve_and_generate(query):
    client = boto3.client("bedrock-agent-runtime", region_name="us-east-1")
    resp = client.retrieve_and_generate(
        input={"text": query},
        retrieveAndGenerateConfiguration={
            "type": "KNOWLEDGE_BASE",
            "knowledgeBaseConfiguration": {
                "knowledgeBaseId": KB_ID,
                "modelArn": MODEL_ARN,
                "retrievalConfiguration": {
                    "vectorSearchConfiguration": {"numberOfResults": NUM_RESULTS}
                },
                "generationConfiguration": {
                    "promptTemplate": {"textPromptTemplate": PROMPT_TEMPLATE}
                },
            },
        },
    )
    return resp["output"]["text"]


def _invoke_agent_fallback(query, request_id):
    client = boto3.client("bedrock-agent-runtime", region_name="us-east-1")
    resp = client.invoke_agent(
        agentId=AGENT_ID,
        agentAliasId=AGENT_ALIAS_ID,
        sessionId="session-" + request_id,
        inputText=query,
    )
    completion = ""
    for stream_event in resp["completion"]:
        if "chunk" in stream_event:
            completion += stream_event["chunk"]["bytes"].decode("utf-8")
    return completion


def lambda_handler(event, context):
    query = _extract_query(event)
    if not query:
        return {"statusCode": 400, "body": json.dumps({"error": "No query provided"})}

    engine = "retrieve_and_generate"
    try:
        answer = _retrieve_and_generate(query)
    except Exception as primary_error:
        # Graceful degradation to the original agent path so a missing
        # bedrock:RetrieveAndGenerate / InvokeModel permission on this Lambda's
        # role never makes the endpoint worse than before.
        print(json.dumps({
            "event": "retrieve_and_generate_failed",
            "error": type(primary_error).__name__,
            "detail": str(primary_error)[:300],
        }))
        engine = "agent_fallback"
        try:
            answer = _invoke_agent_fallback(query, context.aws_request_id)
        except Exception as fallback_error:
            return {
                "statusCode": 500,
                "body": json.dumps({"error": str(fallback_error)}),
            }

    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps({"response": answer, "engine": engine}),
    }
