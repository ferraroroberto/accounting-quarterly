"""Invoice OCR extraction via Google Gemini.

Accepts any PDF (invoice, receipt, ticket, nota de gastos, etc.) and extracts
Spanish-accounting-relevant fields, returning a structured dict ready to store
in the `invoices` table.

Uses the current `google-genai` SDK (google.genai), not the deprecated
`google.generativeai` package.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Optional

from src.logger import get_logger

log = get_logger(__name__)

# Model: gemini-2.0-flash-lite as requested (preview variant)
MODEL = "gemini-3.1-flash-lite-preview"

_EXTRACTION_PROMPT = """
You are an expert Spanish accountant and OCR assistant.

Analyse the attached document (which may be an invoice, receipt, ticket,
delivery note, or any commercial document) and extract the following fields
for Spanish accounting purposes.

Return ONLY a valid JSON object with these keys (use null for missing fields):

{
  "invoice_number":    string | null,
  "invoice_date":      string | null,
  "vendor_name":       string | null,
  "vendor_nif":        string | null,
  "vendor_address":    string | null,
  "client_name":       string | null,
  "client_nif":        string | null,
  "client_address":    string | null,
  "description":       string | null,
  "subtotal_eur":      number | null,
  "iva_rate":          number | null,
  "iva_amount":        number | null,
  "irpf_rate":         number | null,
  "irpf_amount":       number | null,
  "total_eur":         number | null,
  "currency":          string,
  "original_currency": string | null,
  "original_amount":   number | null,
  "fx_rate":           number | null,
  "payment_method":    string | null,
  "category":          string | null,
  "notes":             string | null
}

Field guidance:
- invoice_date: ISO 8601 YYYY-MM-DD format.
- subtotal_eur: base imponible (net amount before taxes), in EUR.
- iva_rate: IVA % (e.g. 21, 10, 4, 0). If not shown but document is Spanish, assume 21.
- iva_amount: cuota IVA in EUR.
- irpf_rate / irpf_amount: IRPF retention % and EUR amount; null if absent.
- total_eur: total amount to pay in EUR.
- currency: document currency (default "EUR").
- original_currency / original_amount: if document is in a non-EUR currency,
  fill these; also try to fill total_eur if a EUR equivalent is shown.
- category: one of TOOLS, SUBSCRIPTIONS, MARKETING, PROFESSIONAL_SERVICES,
  TRAVEL, OFFICE_SUPPLIES, UTILITIES, SOFTWARE, HARDWARE, OTHER.
- notes: flag anything unusual (missing NIF, unclear totals, foreign currency, etc.).
- All monetary amounts must be numbers, not strings. Convert European-format
  numbers (e.g. "1.234,56") to standard floats (1234.56).
- For tickets/receipts with no explicit IVA breakdown, derive subtotal and
  IVA from the total assuming 21% IVA.
- Do NOT include markdown fences. Output ONLY the JSON object.
"""


def extract_invoice(pdf_path: str | Path, api_key: Optional[str] = None) -> dict:
    """Extract accounting data from a PDF using Gemini.

    Args:
        pdf_path: Path to the PDF file.
        api_key:  Google API key. Falls back to GOOGLE_API_KEY env var.

    Returns:
        Parsed dict with extracted fields plus ``_raw_response`` key.

    Raises:
        RuntimeError: If google-genai is not installed, key is missing, or
                      the API call fails.
        FileNotFoundError: If the PDF does not exist.
    """
    try:
        from google import genai
        from google.genai import types as genai_types
    except ImportError as exc:
        raise RuntimeError(
            "google-genai package not installed. "
            "Run: pip install google-genai"
        ) from exc

    key = api_key or os.getenv("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError("GOOGLE_API_KEY is not set in environment or .env file.")

    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    log.info("Extracting %s via Gemini…", pdf_path.name)

    # Three auth modes:
    # 1. GOOGLE_APPLICATION_CREDENTIALS set → Vertex AI with ADC (service account JSON)
    # 2. Key starts with "AIza" → standard Gemini API (AI Studio key)
    # 3. Fallback → try standard Gemini API with whatever key is provided
    adc_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    project = os.getenv("GOOGLE_CLOUD_PROJECT", "accounting-quarterly")
    location = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")

    if adc_path:
        log.info("Using Vertex AI + ADC (project=%s, location=%s)", project, location)
        client = genai.Client(vertexai=True, project=project, location=location)
    else:
        log.info("Using Gemini API with API key")
        client = genai.Client(api_key=key)

    # Embed PDF inline (avoids Files API; works for docs up to ~20 MB)
    with open(pdf_path, "rb") as fh:
        pdf_bytes = fh.read()
    file_hash = hashlib.md5(pdf_bytes).hexdigest()

    response = client.models.generate_content(
        model=MODEL,
        contents=[
            genai_types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
            _EXTRACTION_PROMPT,
        ],
        config=genai_types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
        ),
    )

    raw_text = (response.text or "").strip()
    log.debug("Gemini raw response for %s: %s", pdf_path.name, raw_text[:500])

    # Strip accidental markdown fences
    clean = raw_text
    if clean.startswith("```"):
        lines = clean.splitlines()
        clean = "\n".join(
            line for line in lines if not line.startswith("```")
        ).strip()

    try:
        data = json.loads(clean)
    except json.JSONDecodeError as exc:
        log.error("JSON parse failed for %s: %s\nRaw: %s", pdf_path.name, exc, raw_text)
        raise RuntimeError(
            f"Gemini returned non-JSON for {pdf_path.name}: {exc}"
        ) from exc

    # Normalise numeric fields — Gemini sometimes returns strings like "1.234,56"
    for field in (
        "subtotal_eur", "iva_rate", "iva_amount", "irpf_rate",
        "irpf_amount", "total_eur", "original_amount", "fx_rate",
    ):
        val = data.get(field)
        if isinstance(val, str):
            normalised = val.replace(".", "").replace(",", ".").strip()
            try:
                data[field] = float(normalised)
            except (ValueError, TypeError):
                data[field] = None

    data["_raw_response"] = raw_text
    data["_file_hash"] = file_hash
    data["currency"] = (data.get("currency") or "EUR").upper()
    return data
