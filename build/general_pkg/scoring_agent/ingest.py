"""Document ingestion.

Loads a project's documents (from S3 in production, or a local directory for
testing) and turns every document into text with page markers. Machine-readable
PDFs go through pdfplumber; scanned/image PDFs are detected and OCR'd via AWS
Textract (primary) or a Bedrock-Claude document block (fallback). Downstream
modules reason over the extracted text, so prompts stay uniform and per-model
document-size limits don't leak into the scoring logic.

Expected S3 / directory layout::

    projects/<id>/rfp/...                 the solicitation / RFP
    projects/<id>/proposals/<Vendor>/...  one subfolder per competing vendor
    projects/<id>/scoresheet/...          official FOIA scoresheet (optional)
"""

from __future__ import annotations

import io
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# pdfminer emits a warning per malformed color op; real government PDFs trigger
# hundreds of these. Silence them so they don't flood CloudWatch / stderr.
logging.getLogger("pdfminer").setLevel(logging.ERROR)
logging.getLogger("pdfplumber").setLevel(logging.ERROR)

# scanned-detection thresholds
_SCAN_SAMPLE_PAGES = 5
_SCAN_MIN_CHARS_PER_PAGE = 80
_OCR_CHUNK_PAGES = 15
# OCR is the dominant per-run cost; scanned filings can be hundreds of pages.
# Cap pages/doc (env-tunable) so a single doc can't consume the whole budget.
_MAX_PAGES_PER_DOC = int(os.environ.get("SCORING_MAX_OCR_PAGES_PER_DOC", "60"))

# Non-PDF file types we can extract as UTF-8 text. Everything else (Office
# binaries, archives, images) is skipped rather than mis-OCR'd — trying to feed
# a .zip/.tif to the PDF/OCR path just wastes a Bedrock call and returns noise.
_SUPPORTED_TEXT_EXT = {".txt", ".md", ".csv", ".json", ".log"}


# --------------------------------------------------------------------------- #
# Data structures
# --------------------------------------------------------------------------- #
@dataclass
class Document:
    """A single source document."""

    category: str                      # "rfp" | "proposal" | "scoresheet"
    filename: str
    vendor: Optional[str] = None       # set for proposals
    s3_key: Optional[str] = None
    local_path: Optional[str] = None
    bucket: Optional[str] = None
    size: int = 0                      # bytes, for total-input budgeting

    @property
    def id(self) -> str:
        base = self.filename or (self.s3_key or self.local_path or "doc")
        return os.path.basename(base)

    @property
    def is_pdf(self) -> bool:
        return self.id.lower().endswith(".pdf")


@dataclass
class DocumentText:
    """Extracted text for a document, with per-page segmentation."""

    document: Document
    pages: List[str] = field(default_factory=list)
    method: str = "text"               # "text" | "ocr-textract" | "ocr-bedrock" | "none"

    @property
    def full_text(self) -> str:
        return "\n\n".join(f"[page {i + 1}]\n{p}" for i, p in enumerate(self.pages) if p.strip())

    @property
    def char_count(self) -> int:
        return sum(len(p) for p in self.pages)


@dataclass
class ProjectDocs:
    project_id: str
    rfp_docs: List[Document] = field(default_factory=list)
    proposal_docs: Dict[str, List[Document]] = field(default_factory=dict)
    scoresheet_docs: List[Document] = field(default_factory=list)

    def vendors(self) -> List[str]:
        return sorted(self.proposal_docs.keys())

    def all_documents(self) -> List[Document]:
        docs = list(self.rfp_docs) + list(self.scoresheet_docs)
        for v in self.proposal_docs.values():
            docs.extend(v)
        return docs


# --------------------------------------------------------------------------- #
# Byte access
# --------------------------------------------------------------------------- #
def read_bytes(doc: Document, s3=None) -> bytes:
    if doc.local_path:
        with open(doc.local_path, "rb") as fh:
            return fh.read()
    if doc.s3_key and doc.bucket:
        if s3 is None:
            import boto3

            s3 = boto3.client("s3")
        obj = s3.get_object(Bucket=doc.bucket, Key=doc.s3_key)
        return obj["Body"].read()
    raise ValueError(f"Document {doc.id} has no local_path or s3_key")


# --------------------------------------------------------------------------- #
# PDF text + scanned detection
# --------------------------------------------------------------------------- #
def pdf_page_texts(data: bytes, max_pages: Optional[int] = None) -> List[str]:
    import pdfplumber

    out: List[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        pages = pdf.pages if max_pages is None else pdf.pages[:max_pages]
        for page in pages:
            out.append(page.extract_text() or "")
    return out


def is_scanned(data: bytes) -> bool:
    """Sample the first pages; if they carry almost no extractable text, it's scanned."""
    try:
        sample = pdf_page_texts(data, max_pages=_SCAN_SAMPLE_PAGES)
    except Exception:
        return True
    if not sample:
        return True
    avg = sum(len(t.strip()) for t in sample) / max(len(sample), 1)
    return avg < _SCAN_MIN_CHARS_PER_PAGE


# --------------------------------------------------------------------------- #
# OCR
# --------------------------------------------------------------------------- #
def ocr_textract(doc: Document, s3=None, textract=None, poll_seconds: float = 3.0,
                 timeout_seconds: float = 600.0) -> List[str]:
    """OCR a scanned PDF already in S3 via Textract async text detection.

    Returns text grouped into synthetic "pages" by Textract PAGE blocks.
    """
    if not (doc.s3_key and doc.bucket):
        raise ValueError("Textract OCR requires the document to be in S3")
    import boto3

    textract = textract or boto3.client("textract")
    start = textract.start_document_text_detection(
        DocumentLocation={"S3Object": {"Bucket": doc.bucket, "Name": doc.s3_key}}
    )
    job_id = start["JobId"]
    deadline = time.time() + timeout_seconds
    status = "IN_PROGRESS"
    while status == "IN_PROGRESS" and time.time() < deadline:
        time.sleep(poll_seconds)
        resp = textract.get_document_text_detection(JobId=job_id)
        status = resp["JobStatus"]
    if status != "SUCCEEDED":
        raise RuntimeError(f"Textract job {job_id} ended with status {status}")

    pages: Dict[int, List[str]] = {}
    next_token = None
    while True:
        kwargs = {"JobId": job_id}
        if next_token:
            kwargs["NextToken"] = next_token
        resp = textract.get_document_text_detection(**kwargs)
        for block in resp.get("Blocks", []):
            if block.get("BlockType") == "LINE":
                pg = block.get("Page", 1)
                pages.setdefault(pg, []).append(block.get("Text", ""))
        next_token = resp.get("NextToken")
        if not next_token:
            break
    return ["\n".join(pages[p]) for p in sorted(pages)]


def ocr_bedrock(data: bytes, bedrock, doc_name: str = "scan",
                deadline_epoch: Optional[float] = None) -> List[str]:
    """OCR a PDF by chunking into 15-page segments and transcribing in parallel.

    Deadline-aware: stops submitting new chunks once ``deadline_epoch`` passes and
    returns whatever completed (partial text), so a slow/huge scan can't block the
    worker past its Lambda budget.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from pypdf import PdfReader, PdfWriter

    from .bedrock import DocBlock

    reader = PdfReader(io.BytesIO(data))
    n = min(len(reader.pages), _MAX_PAGES_PER_DOC)
    starts = list(range(0, n, _OCR_CHUNK_PAGES))

    def _transcribe_chunk(start: int) -> tuple:
        writer = PdfWriter()
        for page in reader.pages[start : start + _OCR_CHUNK_PAGES]:
            writer.add_page(page)
        buf = io.BytesIO()
        writer.write(buf)
        text = bedrock.converse(
            "Transcribe ALL text from this document faithfully. Preserve tables as "
            "markdown. Output only the transcription, no commentary.",
            documents=[DocBlock(name=f"{doc_name}-{start}", data=buf.getvalue(), fmt="pdf")],
            fast=True,
            max_tokens=8000,
        )
        return start, text

    results: Dict[int, str] = {}
    with ThreadPoolExecutor(max_workers=min(len(starts), 4)) as pool:
        futures = {}
        for s in starts:
            if deadline_epoch is not None and time.time() > deadline_epoch:
                break  # out of budget — don't start more chunks
            futures[pool.submit(_transcribe_chunk, s)] = s
        for fut in as_completed(futures):
            start, text = fut.result()
            results[start] = text
    # return in page order; only the chunks that completed
    return [results[s] for s in sorted(results)]


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
def extract_text(doc: Document, *, s3=None, textract=None, bedrock=None,
                 deadline_epoch: Optional[float] = None) -> DocumentText:
    """Return the document's text, OCR'ing scanned PDFs when needed.

    OCR priority: Bedrock vision (no async polling, fast) → Textract (fallback for
    very large docs that exceed Bedrock's per-chunk limit).

    Unsupported binary types (Office, archives, images) are skipped up front so
    we never download them or waste an OCR call. If ``deadline_epoch`` is set and
    already passed, scanned PDFs are skipped rather than started — this keeps the
    async worker from blowing its Lambda time budget mid-OCR.
    """
    ext = os.path.splitext(doc.id)[1].lower()
    if not doc.is_pdf and ext not in _SUPPORTED_TEXT_EXT:
        return DocumentText(document=doc, pages=[], method="skipped")

    data = read_bytes(doc, s3=s3)

    if not doc.is_pdf:
        try:
            return DocumentText(document=doc, pages=[data.decode("utf-8", errors="replace")], method="text")
        except Exception:
            return DocumentText(document=doc, pages=[], method="none")

    if not is_scanned(data):
        return DocumentText(document=doc, pages=pdf_page_texts(data, max_pages=_MAX_PAGES_PER_DOC), method="text")

    # scanned PDF: OCR is the expensive path — bail if we're already out of budget
    if deadline_epoch is not None and time.time() > deadline_epoch:
        return DocumentText(document=doc, pages=[], method="skipped-deadline")

    # scanned → Bedrock vision first (synchronous, no polling delay)
    if bedrock is not None:
        try:
            pages = ocr_bedrock(data, bedrock, doc.id, deadline_epoch=deadline_epoch)
            if pages:
                return DocumentText(document=doc, pages=pages, method="ocr-bedrock")
        except Exception:
            pass

    # out of budget after Bedrock attempt — don't start a slow Textract job
    if deadline_epoch is not None and time.time() > deadline_epoch:
        return DocumentText(document=doc, pages=[], method="skipped-deadline")

    # Bedrock failed (e.g. doc too large after chunking) → Textract async fallback
    if doc.s3_key and doc.bucket:
        try:
            return DocumentText(document=doc,
                                pages=ocr_textract(doc, s3=s3, textract=textract, timeout_seconds=180.0),
                                method="ocr-textract")
        except Exception:
            pass

    return DocumentText(document=doc, pages=[], method="none")


# --------------------------------------------------------------------------- #
# Project loading
# --------------------------------------------------------------------------- #
def _categorize(rel_path: str) -> tuple:
    """Map a relative path under projects/<id>/ to (category, vendor)."""
    parts = [p for p in rel_path.replace("\\", "/").split("/") if p]
    if not parts:
        return None, None
    head = parts[0].lower()
    if head == "rfp":
        return "rfp", None
    if head in ("scoresheet", "scoresheets"):
        return "scoresheet", None
    if head in ("proposals", "proposal", "vendors"):
        vendor = parts[1] if len(parts) > 1 else "Unknown"
        return "proposal", vendor
    return None, None


def load_project_from_s3(bucket: str, project_id: str, s3=None) -> ProjectDocs:
    if s3 is None:
        import boto3

        s3 = boto3.client("s3")
    prefix = f"projects/{project_id}/"
    proj = ProjectDocs(project_id=project_id)
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            rel = key[len(prefix):]
            category, vendor = _categorize(rel)
            if category is None:
                continue
            doc = Document(category=category, filename=os.path.basename(key),
                           vendor=vendor, s3_key=key, bucket=bucket,
                           size=int(obj.get("Size", 0) or 0))
            _add(proj, doc)
    return proj


def load_project_from_dir(root: str, project_id: Optional[str] = None) -> ProjectDocs:
    """Load a project from a local directory laid out like the S3 layout."""
    project_id = project_id or os.path.basename(os.path.normpath(root))
    proj = ProjectDocs(project_id=project_id)
    for dirpath, _dirs, files in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        for name in files:
            if name.startswith("."):
                continue
            rel = os.path.join("" if rel_dir == "." else rel_dir, name)
            category, vendor = _categorize(rel)
            if category is None:
                continue
            full = os.path.join(dirpath, name)
            try:
                fsize = os.path.getsize(full)
            except OSError:
                fsize = 0
            doc = Document(category=category, filename=name, vendor=vendor,
                           local_path=full, size=fsize)
            _add(proj, doc)
    return proj


def check_staging_manifest(scoring_bucket: str, project_id: str, s3=None) -> Optional[dict]:
    """Return the CI staging manifest for this project if it exists, else None."""
    if s3 is None:
        import boto3
        s3 = boto3.client("s3")
    key = f"projects/{project_id}/.ci_manifest.json"
    try:
        obj = s3.get_object(Bucket=scoring_bucket, Key=key)
        return json.loads(obj["Body"].read())
    except Exception:  # noqa: BLE001
        return None


def _add(proj: ProjectDocs, doc: Document) -> None:
    if doc.category == "rfp":
        proj.rfp_docs.append(doc)
    elif doc.category == "scoresheet":
        proj.scoresheet_docs.append(doc)
    elif doc.category == "proposal":
        proj.proposal_docs.setdefault(doc.vendor or "Unknown", []).append(doc)
