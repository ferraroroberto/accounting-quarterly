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
You are an expert Spanish accountant and OCR assistant specialising in AEAT compliance.

Analyse the attached document (which may be a factura completa, factura simplificada,
ticket, recibo, nota de gastos, delivery note, or any commercial document) and extract
ALL fields required for Spanish accounting and AEAT filings (Libro de IVA, Modelo 303,
Modelo 130, Modelo 347, Modelo 349, SII).

Return ONLY a valid JSON object with these keys (use null for missing/inapplicable fields):

{
  "invoice_number":         string | null,
  "invoice_date":           string | null,
  "supply_date":            string | null,
  "due_date":               string | null,
  "invoice_type":           string | null,
  "is_rectificativa":       boolean,
  "rectified_invoice_ref":  string | null,
  "vendor_name":            string | null,
  "vendor_nif":             string | null,
  "vendor_address":         string | null,
  "client_name":            string | null,
  "client_nif":             string | null,
  "client_address":         string | null,
  "description":            string | null,
  "billing_period_start":   string | null,
  "billing_period_end":     string | null,
  "subtotal_eur":           number | null,
  "iva_rate":               number | null,
  "iva_amount":             number | null,
  "iva_breakdown":          array | null,
  "irpf_rate":              number | null,
  "irpf_amount":            number | null,
  "total_eur":              number | null,
  "currency":               string,
  "original_currency":      string | null,
  "original_amount":        number | null,
  "fx_rate":                number | null,
  "payment_method":         string | null,
  "vat_exempt_reason":      string | null,
  "deductible_pct":         number | null,
  "category":               string | null,
  "notes":                  string | null
}

Field guidance:

DATES (all ISO 8601 YYYY-MM-DD):
- invoice_date: date printed on the document.
- supply_date: fecha de prestación/entrega — when goods or services were actually
  delivered. Fill only if explicitly different from invoice_date; otherwise null.
- due_date: fecha de vencimiento / payment due date. Extract from payment terms
  (e.g. "30 días netos" → add 30 days to invoice_date).
- billing_period_start / billing_period_end: for subscription or recurring invoices
  that state a coverage period (e.g. "Periodo: 01/01/2024 – 31/03/2024").

DOCUMENT TYPE (invoice_type):
- "factura_completa"     — standard full invoice (has NIF, address, itemised taxes)
- "factura_simplificada" — simplified invoice (ticket-style, NIF may be absent)
- "ticket"               — till receipt, no NIF required
- "recibo"               — receipt for payment already made
- "nota_gastos"          — expense note / nota de gastos
- "factura_proforma"     — pro-forma invoice (not a tax document)
- "other"                — anything else

CORRECTIONS:
- is_rectificativa: true if this is a factura rectificativa (corrective invoice).
- rectified_invoice_ref: original invoice number/series being corrected, if stated.

AMOUNTS (all numbers, no strings — convert "1.234,56" → 1234.56):
- subtotal_eur: base imponible total (sum of all taxable bases), in EUR.
- iva_rate / iva_amount: use the MAIN or ONLY IVA rate/amount.
  If there is a single rate, fill both fields.
- iva_breakdown: REQUIRED when the invoice has multiple IVA rates (very common in Spain).
  Array of objects, one per tax line:
  [
    {
      "base_imponible": number,
      "iva_rate": number,
      "iva_amount": number,
      "re_rate": number | null,
      "re_amount": number | null
    }
  ]
  re_rate / re_amount: recargo de equivalencia (retail surcharge), if applicable.
  If only one rate exists, still fill iva_breakdown with one element.
  If no explicit breakdown is visible, derive it from the total.
- irpf_rate / irpf_amount: IRPF retention % and amount (negative from total).
  Common rates: 15% (professionals), 7% (new activity first 3 years), 19% (rent).
  If not shown, set both to null — do NOT assume.
- total_eur: final amount payable (subtotal + IVA − IRPF), in EUR.
- currency: document currency, default "EUR".
- original_currency / original_amount / fx_rate: if amounts are in a foreign currency.

VAT TREATMENT:
- vat_exempt_reason: if IVA = 0% or exempt, state the legal basis if shown
  (e.g. "Art. 20 LIVA", "Art. 25 LIVA exportación", "operación intracomunitaria",
  "OSS", "Art. 7.1 LIVA"). null if standard rated.

DEDUCTIBILITY:
- deductible_pct: percentage of this expense deductible for IRPF/IVA purposes.
  Default 100 for normal business expenses. Use 50 for mixed-use vehicles, meals
  with limited deductibility, or home-office partial use. null means unknown.

PAYMENT:
- payment_method: "transferencia", "tarjeta", "efectivo", "domiciliación",
  "cheque", "paypal", "stripe", or other text found in the document.

CATEGORY (classify the expense/income):
  TOOLS, SUBSCRIPTIONS, MARKETING, PROFESSIONAL_SERVICES, TRAVEL,
  OFFICE_SUPPLIES, UTILITIES, SOFTWARE, HARDWARE, RENT, INSURANCE,
  BANKING_FEES, TRAINING, MEALS, OTHER.

NOTES:
- Flag: missing NIF on full invoice, amounts that don't add up, foreign currency
  without FX rate, unclear IVA treatment, potential recargo de equivalencia,
  intracomunitaria transactions, exports, possible SII obligation.
- Always note if the document appears to be outside the ordinary Spanish VAT regime.

QUALITY RULES:
- All monetary fields must be numbers (float), never strings.
- subtotal_eur + iva_amount − irpf_amount should equal total_eur (verify mentally).
- For tickets with no IVA breakdown, derive: subtotal = total / 1.21, iva_amount = total − subtotal.
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
        "deductible_pct",
    ):
        val = data.get(field)
        if isinstance(val, str):
            normalised = val.replace(".", "").replace(",", ".").strip()
            try:
                data[field] = float(normalised)
            except (ValueError, TypeError):
                data[field] = None

    # Normalise iva_breakdown numeric sub-fields
    breakdown = data.get("iva_breakdown")
    if isinstance(breakdown, list):
        for line in breakdown:
            if isinstance(line, dict):
                for sub in ("base_imponible", "iva_rate", "iva_amount", "re_rate", "re_amount"):
                    v = line.get(sub)
                    if isinstance(v, str):
                        try:
                            line[sub] = float(v.replace(".", "").replace(",", ".").strip())
                        except (ValueError, TypeError):
                            line[sub] = None
        data["iva_breakdown"] = breakdown

    # Coerce is_rectificativa to bool
    data["is_rectificativa"] = bool(data.get("is_rectificativa"))

    data["_raw_response"] = raw_text
    data["_file_hash"] = file_hash
    data["currency"] = (data.get("currency") or "EUR").upper()
    return data
