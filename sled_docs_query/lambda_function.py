import boto3
import json
import re

# Knowledge base backing the SLED competitive-intelligence corpus
# (data source = s3://competitive-intelligence-sled).
KB_ID = "FIFNL0U11I"
MODEL_ARN = (
    "arn:aws:bedrock:us-east-1:211125468742:"
    "inference-profile/us.anthropic.claude-sonnet-4-20250514-v1:0"
)
NUM_RESULTS = 10

# Fallback: the original Bedrock Agent (kept so a permissions gap degrades
# gracefully to the previous behavior, never worse).
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

# ── Corpus catalog ────────────────────────────────────────────────────────────
# Snapshot of the S3 folder structure (vendors -> procurements). RAG can't
# enumerate folder names (it only matches document text), so questions like
# "who are IBM's competitors" or "what procurements do you have" are answered
# from this catalog instead. Regenerate + redeploy when the bucket structure
# changes (see README).
CATALOG = json.loads(r"""{
"vendors": {
"AHEAD": [
"AHEAD  CA CAIR3 RFI Response"
],
"AST Corp": [
"Applications Software Tech Santa Clara ERP Proposal",
"Chicago Housing Authority (2022)",
"FL Broward (2013)",
"FL Tampa Hillsborough (eBiz Suite) (2011)",
"Public Sector Oracle Cloud Competitor Proposals (from AST)",
"VA Loudon County (2012)"
],
"Accenture": [
"AR IE-BM (2018) (HHS)",
"AZ AFIS (ERP) (2013)",
"Accenture CA CAIR3 RFI Response",
"Accenture Contracts with CDPH (California)",
"Accenture FL CCWIS 2022 Proposal",
"Accenture Santa Clara ERP Proposal",
"Accenture_BAFO_Proposal AZ Dept of Child Safety",
"CA CDCR S4 HANA Migration 2024 FOIA",
"CA FI$Cal (ERP) (2011)",
"CA HBX (2012)",
"CA MyCalPays (HCM) (2009)",
"CA NY Integrated Eligibility",
"CALPERS - Pension System Replacement Resumption (2006)",
"FL Child Welfare",
"FL DOT FTE nBOS RFI (2019)",
"FL Integrated Eligibility M&O (2012)",
"FL Tampa Hillsborough ERP (2011)",
"Federal IDIQ Alliant",
"GA IES (2014)",
"IA Eligibility (2012)",
"ID Child Welfare",
"IL IES",
"IL Municipal Retirement Fund (IMRF - 2012)",
"IL Tolling (2016)",
"KS ERP (2009)",
"MI ERP (2014)",
"MN Data Analytics",
"MO ERP Evaluation and Signed Contract (2022)",
"Missouri Department of Administration (ERP Solution Implementation Services)",
"NC FAST (2010)",
"Ohio OAKS ERP (2006)",
"PA Turnpike",
"Tolling",
"TxDOT BOS (2018)",
"WVA ERP (2011)",
"WY Eligibility System (2012)",
"WY MMIS Proposal",
"WY WINGS Medicaid SI (2017)",
"Washington D.C Office (CFOPD-19-R-001.  Enterprise Financial System) 2018.2019",
"ZZ - General Sales - Road Charging (2011)"
],
"AdvoLogix": [
"FL DEPT OF CORRECTIONS OBIS (2021)"
],
"Akido Labs": [
"ZZ - Information on Key Akido Labs Leadership"
],
"Amzur Technologies, Inc□": [
"FL DEPT OF CORRECTIONS OBIS (2021)"
],
"Atos": [
"VITA Security"
],
"Bahwan CyberTek Inc Santa Clara ERP Proposal": [],
"Binti Inc": [
"Binti Inc FL CCWIS 2022 Proposal"
],
"Black and Veatch Tampa Hillsborough (2011)": [
"Black and Veatch"
],
"Business Integra": [],
"CGI": [
"CGI Technologies and Solutions Inc FL CCWIS 2022 Proposal",
"FL Child Welfare",
"Pre 2025 CGI"
],
"CHMRC": [
"CHMRC  CA CAIR3 RFI Response"
],
"Capgemini": [
"FDOT nBOS RFI (2019)"
],
"Cardinality": [],
"Case Commons": [
"CA CWS-NS 2017"
],
"CaseNet": [
"California Population Health Proposal"
],
"Cedar Crestone": [
"FL Broward (2014)",
"FL Tampa Hillsborough (2011)",
"IL Municipal Retirement Fund (IMRF 2012)",
"U of MN (2013)"
],
"CherryRoad": [
"FL Tampa Hillsborough (ERP - 2011)",
"WI (PS ERP - 2013)",
"WVA (ERP - 2011)"
],
"Ciber": [
"FL Broward (ERP 2013)",
"FL Tampa Hillsborough ERP (2011)",
"FL Tolling (2020)",
"MD Montgomery Cnty ERP",
"WA King County (2012)",
"WA SBCTC",
"ZZ Misfiled - Jenny to move"
],
"Cognizant": [
"MA Tolling",
"TX Tolling",
"TxDOT BOS (2018)"
],
"Collaborative Solutions (Workday)": [
"Collaborative State of Washington (OneWa) Workday 2020"
],
"Conduent": [
"FL Tolling",
"MD Tolling",
"MI Tolling",
"NJ Tolling",
"NY Tolling"
],
"Conduent (Formerly Xerox State and Local Solutions)": [
"Conduent Contract Information and Response_FL SunPass",
"FL SunPass (2015)",
"Maryland Congestion Relief (2017)",
"Michigan Toll Bridge (2012)",
"NH EZPass Tolling BOS (2015)",
"New Jersey EZ-Pass CSC Contract (2015)",
"SC Contact Center Proposal (2016)",
"TxDOT BOS (2018)"
],
"Corespere": [
"Corespere LLC FL CCWIS 2022 Proposal"
],
"Creative Information Technology": [
"Creative Information Technology Inc FL CCWIS 2022 Proposal"
],
"Cubic": [
"FDOT nBOS RFI (2019)",
"NH EZPass Tolling BOS (2015)"
],
"DXC": [
"FL MMIS DXC Proposal"
],
"Delaware EDW FOIA Scoring": [],
"Dell": [
"WY Eligibility (2012)"
],
"Deloitte": [
"AK DHSS Eligiblity - Medicaid (2012)",
"AR Human Services M&O (2017)",
"AR IE-BM (2018)",
"CA - City of San Diego ADMI (2018)",
"CA CDCR S4 HANA Migration 2024 FOIA",
"CA CWS-NS (2017)",
"CA Dept of Child Support Services Migration to Azure",
"CA EDD ICMS RFP 3362",
"CA Orange County (2019)",
"California Population Health Proposal",
"Child Welfare Deloitte Contracts and Orals",
"DE FACTS II Child Welfare (2012)",
"Deloitte  CA CAIR3 RFI Response",
"Deloitte Contract",
"Deloitte FL CCWIS 2022 Proposal",
"Deloitte State of Washington Workday (OneWA) 2020",
"Deloitte's Contracts with CDPH (California)",
"FL Child Welfare",
"FL DEPT OF CORRECTIONS OBIS (2021)",
"FL Integrated Eligibility M&O (2012)",
"FL MMIS",
"Georgia IE (Contract and Proposal)",
"IA Eligibility (2012)",
"ID Child Welfare",
"ID Child Welfare (2018)",
"IL Child Welfare",
"IL DHFS Child Support (2013)",
"IL Medicaid RFI (2014)",
"IN IES (2012)",
"Iowa DHS Medicaid Program Integrity Professional Services (2024)",
"KY CHFS",
"LA Medicaid Eligibility (2015)",
"LA Rate Card",
"MI Bridges (2017) & 2010 Evaluation",
"MI ERP (2014)",
"MI HIE Hub (2013)",
"MN Data Analytics (2014)",
"MO ERP Evaluation (2022)",
"MT Chimes (2009)",
"OH MES (Medicaid) SI 2018",
"Orange County ERP Deloitte",
"RI UHIP Contract  & Assessment (2012)",
"TN PI RFP (2022)",
"TN TennCare 2012",
"TX DIR-CPO (2025)",
"VA MES (Medicaid) SI 2018",
"WA ACES M&O for DSHS",
"WVA DHHR IES (2017)",
"WY HIEES (2012)",
"WY Integrated Eligibility M&O (2016)",
"WY MMIS",
"WY MMIS Contract",
"WY MMIS Proposal",
"WY Medicaid SI (WINGS) (2017)",
"Washington CCWIS Modernization 2025 (Awarded)",
"Washington D.C Office (CFOPD-19-R-001.  Enterprise Financial System) 2018.2019"
],
"Denovo": [
"Denovo Ventures LLC Santa Clara ERP Proposal"
],
"Digital Management": [
"Digital Management LLC FL CCWIS 2022 Proposal"
],
"Diona": [
"AZ CW Solution Mobile (2016)"
],
"ETCC": [
"FDOT nBOS RFI (2019)"
],
"Engagepoint": [
"Missouri IES",
"WY MES (Medicaid) SI (2017)"
],
"Engility - DRC": [],
"Ernst & Young": [
"E and Y  CA CAIR3 RFI Response"
],
"Etan Industries LLC": [
"UT Tolling"
],
"FORWARD": [],
"Faneuil": [
"FDOT nBOS RFI (2019)"
],
"Fieldware, LLC": [
"FL DEPT OF CORRECTIONS OBIS (2021)"
],
"GDIT": [
"Alabama MMIS Contract",
"FL MMIS GDIT Proposal"
],
"Gainwell": [
"California Population Health Proposal",
"Gainwell"
],
"Gila d_b_a MSB": [
"TxDOT BOS (2018)"
],
"Google": [
"Google  CA CAIR3 RFI Response"
],
"Guidehouse": [
"Guidehouse FL CCWIS 2022 Proposal"
],
"GxP Partners, LLC": [
"FL DEPT OF CORRECTIONS OBIS (2021)"
],
"HCL": [
"HCL America Inc Santa Clara ERP Proposal",
"IA Integrated Eligibility (2012)"
],
"HOTB": [],
"HP (& EDS)": [
"CA CMIPS Original EDS Proposal",
"CA SAWS M&O (2016)",
"CA SOMS (2009)",
"CMIPS II (2008) - contract & amendments",
"COSD ITO",
"Irvine (2016)",
"NJ CASS"
],
"HealthNet": [
"Arizona MMIS"
],
"Houston Technologies LLC": [
"FL DEPT OF CORRECTIONS OBIS (2021)"
],
"IBM Scoring": [
"FL MMIS IBM Proposal",
"IBM FL CCWIS 2022 Proposal",
"IBM Santa Clara ERP Proposal",
"Santa Clara Valley Transit ERP Contract",
"WA CCWIS Proposal"
],
"IBM Tampa": [
"Financials",
"Original RFP"
],
"Infosys": [
"AR IE-BM (2018)",
"MO ERP Evaluation (2022)",
"SC CGIS 2016",
"SC CGIS 2017"
],
"Intellias (Infor)": [
"Chicago Housing Authority (2022)"
],
"Ithena": [],
"KAI": [
"KAI  CA CAIR3 RFI Response"
],
"KPMG": [
"KPMG FL CCWIS 2022 Proposal",
"MO ERP",
"Washington D.C Office of Contracts and Procurement (CFOPD-19-R-001.  Enterprise Financial System)"
],
"Kapsch": [
"TxDOT BOS (2018)"
],
"Kapsch Trafficcom": [
"LOUISVILLE-SOUTHERN INDIANA OHIO RIVER BRIDGES TOLL"
],
"LexisNexis": [
"LexisNexis  CA CAIR3 RFI Response"
],
"MTX": [
"NM Contact Tracing",
"Nebraska Licensing",
"TX Contact Tracing"
],
"Marquis Software": [
"FL DEPT OF CORRECTIONS OBIS (2021)"
],
"McKinsey": [
"McKinsey  CA CAIR3 RFI Response",
"McKinsey's Contracts with CDPH (California)"
],
"Medicision": [
"ZZ - Leadership"
],
"Mi-Case": [
"FL DEPT OF CORRECTIONS OBIS (2021)"
],
"Micro Focus Inc□": [
"FL DEPT OF CORRECTIONS OBIS (2021)"
],
"Microsoft": [
"AZ CW Solution Mobile (2016)"
],
"Northrop Grumman": [
"AR Human Services M&O (2017)",
"AR M&O 2017",
"LA IT Services Rates (2014)",
"RI UHIP Eligiblity (2012)",
"TennCares Medicaid Eligibility (2012)",
"WY Eligibility (2016)"
],
"Northwoods": [
"AZ CW Solution Mobile (2016)"
],
"eSystems": [
"AR EEF eSystems Rates",
"FED GSA Proposal & Rates",
"SC Curam CGIS (2016)",
"SC eSystems Rates to IBM",
"WY Eligibility"
]
},
"collections": {
"00_FOIA Analysis": [],
"01_NEW 2026": [
"(Partial Documents) Sound Transit ERP Platform",
"(Partial Recieved) BAIFA's Next Generation Express Lanes Toll System Open Host Data Platform",
"AL Data Warehouse_2023",
"BATA (FasTrak® Regional Customer Service Center)",
"California DHCS (HR + Modernization) Workday 151073",
"California DHCS RFP 23-063",
"California Dental Medicaid Management Information System Fiscal Intermediary",
"California Department of Corrections and Rehabilitation (RFP C5608110-D□ CDCR SOMS M&O)",
"California Department of Corrections and Rehabilitation( C5607998-D CDCR PaaS for SOMS Hosting)",
"Child Welfare",
"City of Los Angeles",
"FL Broward County",
"Florida Broward County",
"Illinois IES",
"KPMG",
"Kentucky COT AI",
"Medicaid",
"NC Department of Health & Human Services (sole source)",
"New Jersey Turnpike EZ Pass Back Office",
"New York City Office for People with Developmental Disabilities (OPWDD)",
"Old",
"State Roadway Toll Authority (SRTA) Toll Integration Services Contractor (TISC)",
"TX DOT Toll Operations Contract (CSC 2013)",
"Texas Department of Information Resources (RFO Number□ DIR-CPO-TMP-550)",
"Washington D.C Office of Contracts and Procurement (CFOPD-19-R-001.  Enterprise Financial System)"
],
"22nd Century": [
"CA Dept of Child Support Services Migration to Azure"
]
}
}""")

_VENDORS = CATALOG.get("vendors", {})
_COLLECTIONS = CATALOG.get("collections", {})


def _match_vendor(query):
    ql = query.lower()
    best = None
    for v in _VENDORS:
        base = v.split("(")[0].strip().lower()
        if len(base) < 3:
            continue
        if re.search(r"\b" + re.escape(base) + r"\b", ql):
            if best is None or len(base) > len(best[1]):
                best = (v, base)
    return best[0] if best else None


def _catalog_intent(query):
    ql = " " + re.sub(r"[^a-z0-9]+", " ", query.lower()) + " "
    if any(w in ql for w in (" won ", " win ", " wins ", " lost ", " loss ", " awarded ", " award ")):
        return (None, None)  # outcome/win-loss questions -> let RAG answer honestly
    vendor = _match_vendor(query)
    asks_vendors = any(w in ql for w in (
        " competitor ", " competitors ", " compete ", " competes ", " competing ",
        " vendor ", " vendors ", " companies ", " firms ", " players "))
    asks_catalog = (any(w in ql for w in (" catalog ", " coverage ", " corpus ", " knowledge base "))
                    or " what is in " in ql or " whats in " in ql or " what do you have " in ql
                    or " what is available " in ql or " whats available " in ql)
    asks_proc = any(w in ql for w in (
        " procurement ", " procurements ", " deal ", " deals ", " project ", " projects ",
        " engagement ", " engagements ", " opportunity ", " opportunities ",
        " rfp ", " rfps ", " solicitation ", " solicitations "))
    if vendor and asks_proc:
        return ("vendor_procurements", vendor)
    if asks_vendors or asks_catalog:
        return ("vendors", None)
    if asks_proc:
        return ("procurements", None)
    return (None, None)


def _total_procurements():
    return (sum(len(p) for p in _VENDORS.values())
            + sum(len(p) for p in _COLLECTIONS.values()))


def _format_vendors():
    competitors = sorted(v for v in _VENDORS if not v.upper().startswith("IBM"))
    lines = [
        "Based on the SLED competitive-intelligence knowledge base contents "
        f"(organized by vendor), it tracks {len(competitors)} competitor/vendor "
        f"organizations plus IBM's own materials, across ~{_total_procurements()} "
        "procurements/engagements.",
        "",
        "Competitors / vendors on file:",
        ", ".join(competitors) + ".",
        "",
        "Ask about any one (e.g. \"What procurements does Deloitte have?\") to see "
        "its specific deals and engagements, or ask me to score/analyze a named deal.",
    ]
    return "\n".join(lines)


def _format_vendor_procurements(vendor):
    procs = _VENDORS.get(vendor) or _COLLECTIONS.get(vendor) or []
    if not procs:
        return (f"\"{vendor}\" is tracked in the knowledge base, but its documents sit "
                "at the top level with no sub-procurement folders catalogued. Ask a "
                "specific question about it and I'll search the documents directly.")
    lines = [f"{vendor} - {len(procs)} procurement(s)/engagement(s) on file in the knowledge base:"]
    lines += [f"  - {p}" for p in procs]
    return "\n".join(lines)


def _format_procurements():
    counts = sorted(((len(p), v) for v, p in _VENDORS.items() if p), reverse=True)
    lines = [
        f"The knowledge base holds ~{_total_procurements()} procurements/engagements "
        f"across {len(_VENDORS)} vendors. Count by vendor:",
    ]
    lines += [f"  - {v}: {n}" for n, v in counts]
    if _COLLECTIONS:
        lines.append("")
        lines.append("Additional procurement collections:")
        for c, p in sorted(_COLLECTIONS.items()):
            lines.append(f"  - {c}: {len(p)}")
    lines.append("")
    lines.append("Ask about a specific vendor to list its individual procurements.")
    return "\n".join(lines)


def _catalog_answer(query):
    kind, vendor = _catalog_intent(query)
    if kind == "vendor_procurements":
        return _format_vendor_procurements(vendor)
    if kind == "vendors":
        return _format_vendors()
    if kind == "procurements":
        return _format_procurements()
    return None


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

    # Enumeration questions (who are the competitors / what procurements exist) are
    # answered from the folder catalog, which RAG cannot see.
    catalog = _catalog_answer(query)
    if catalog is not None:
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"response": catalog, "engine": "catalog"}),
        }

    engine = "retrieve_and_generate"
    try:
        answer = _retrieve_and_generate(query)
    except Exception as primary_error:
        print(json.dumps({
            "event": "retrieve_and_generate_failed",
            "error": type(primary_error).__name__,
            "detail": str(primary_error)[:300],
        }))
        engine = "agent_fallback"
        try:
            answer = _invoke_agent_fallback(query, context.aws_request_id)
        except Exception as fallback_error:
            return {"statusCode": 500, "body": json.dumps({"error": str(fallback_error)})}

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps({"response": answer, "engine": engine}),
    }
