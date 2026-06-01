"""Invoice OCR extraction from PDFs for Spanish accounting.

Accepts any PDF (invoice, receipt, ticket, nota de gastos, etc.) and extracts
Spanish-accounting-relevant fields, returning a structured dict ready to store
in the `invoices` table.

Two backends are supported, selected by the ``provider`` argument /
``invoice_ocr.provider`` config key / ``INVOICE_OCR_PROVIDER`` env var:

- ``"hub"`` (default) — routes the PDF + prompt through the local-llm-hub
  (http://127.0.0.1:8000) using the Anthropic SDK and a ``document`` content
  block, mapped to the ``gemini_pro`` alias. No Google credentials needed.
- ``"gemini"`` — the legacy direct path via the ``google-genai`` SDK, using a
  Vertex AI ADC service account or a Gemini API key. Kept as a fallback.

Both paths share the same extraction prompt and post-parsing, so the returned
dict schema is identical regardless of provider.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Optional

from src.logger import get_logger

log = get_logger(__name__)

# Direct google-genai model id (legacy "gemini" provider path).
MODEL = "gemini-3.1-flash-lite-preview"

# local-llm-hub defaults (the "hub" provider path).
HUB_BASE_URL = "http://127.0.0.1:8000"
HUB_MODEL = "gemini_pro"  # stable alias — never the display name

# Default provider for extraction. Override via config (invoice_ocr.provider)
# or the INVOICE_OCR_PROVIDER env var.
#
# Defaults to "hub": every LLM call flows through local-llm-hub for central LAN
# access and observability, and no Google credentials are needed on this machine.
# The hub's PDF-attachment reliability bug (local-llm-hub#63) is fixed — the hub
# now passes attachment dirs via `agy --add-dir`, so document/PDF blocks ingest
# deterministically. The legacy "gemini" (Vertex / API-key) path remains fully
# intact as a selectable fallback.
DEFAULT_PROVIDER = "hub"

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


def _resolve_provider(provider: Optional[str]) -> str:
    """Pick the extraction backend: explicit arg › env › config › default."""
    if provider:
        return provider.strip().lower()
    env = os.getenv("INVOICE_OCR_PROVIDER")
    if env:
        return env.strip().lower()
    try:
        from src.config import load_config

        cfg_provider = load_config().get("invoice_ocr", {}).get("provider")
        if cfg_provider:
            return str(cfg_provider).strip().lower()
    except Exception:  # config optional — fall through to default
        log.debug("Could not read invoice_ocr.provider from config; using default.")
    return DEFAULT_PROVIDER


def _parse_json_object(raw_text: str) -> dict:
    """Parse the JSON object out of a model response.

    Tolerates markdown fences and any prose the model may emit before or after
    the object. The legacy ``gemini`` path constrains output with Gemini's
    ``response_mime_type="application/json"``; the ``hub`` path cannot pass that
    backend-specific flag, so the model occasionally wraps the JSON in fences or
    appends a trailing comment. We first try a strict parse, then fall back to
    extracting the first balanced top-level ``{...}`` object.
    """
    clean = raw_text.strip()

    # Strip wrapping markdown fences if present.
    if clean.startswith("```"):
        lines = clean.splitlines()
        clean = "\n".join(
            line for line in lines if not line.startswith("```")
        ).strip()

    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        pass

    # Fall back: scan for the first balanced top-level object, ignoring braces
    # inside string literals.
    start = clean.find("{")
    if start == -1:
        raise ValueError("no JSON object found in response")
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(clean)):
        ch = clean[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(clean[start : i + 1])
    raise ValueError("unterminated JSON object in response")


def _extract_via_hub(pdf_bytes: bytes, pdf_name: str) -> str:
    """Send the PDF + prompt through local-llm-hub; return the raw model text.

    Uses the Anthropic SDK shape with a ``document`` content block, routed to
    the ``gemini_pro`` alias. The hub is the standard LAN entry point; do not
    re-implement a CLI subprocess wrapper here.
    """
    try:
        from anthropic import Anthropic
    except ImportError as exc:
        raise RuntimeError(
            "anthropic package not installed. Run: pip install anthropic"
        ) from exc

    base_url = os.getenv("LLM_HUB_BASE_URL", HUB_BASE_URL)
    model = os.getenv("LLM_HUB_MODEL", HUB_MODEL)
    log.info("Extracting %s via local-llm-hub (%s, model=%s)…", pdf_name, base_url, model)

    client = Anthropic(api_key="local-dummy", base_url=base_url)
    pdf_b64 = base64.b64encode(pdf_bytes).decode("ascii")
    response = client.messages.create(
        model=model,
        max_tokens=8192,
        temperature=0,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_b64,
                        },
                    },
                    {"type": "text", "text": _EXTRACTION_PROMPT},
                ],
            }
        ],
    )
    parts = [block.text for block in response.content if getattr(block, "type", None) == "text"]
    return "".join(parts).strip()


def _extract_via_gemini(pdf_bytes: bytes, pdf_name: str, api_key: Optional[str]) -> str:
    """Send the PDF + prompt directly to Gemini; return the raw model text.

    Legacy fallback path via ``google-genai`` (Vertex ADC or API key).
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

    # Three auth modes:
    # 1. GOOGLE_APPLICATION_CREDENTIALS set → Vertex AI with ADC (service account JSON)
    # 2. Key starts with "AIza" → standard Gemini API (AI Studio key)
    # 3. Fallback → try standard Gemini API with whatever key is provided
    adc_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    project = os.getenv("GOOGLE_CLOUD_PROJECT", "accounting-quarterly")
    location = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")

    if adc_path:
        log.info(
            "Extracting %s via Vertex AI + ADC (project=%s, location=%s)…",
            pdf_name, project, location,
        )
        client = genai.Client(vertexai=True, project=project, location=location)
    else:
        if not key:
            raise RuntimeError(
                "GOOGLE_API_KEY is not set in environment or .env file."
            )
        log.info("Extracting %s via Gemini API with API key…", pdf_name)
        client = genai.Client(api_key=key)

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
    return (response.text or "").strip()


def extract_invoice(
    pdf_path: str | Path,
    api_key: Optional[str] = None,
    provider: Optional[str] = None,
) -> dict:
    """Extract accounting data from a PDF.

    Args:
        pdf_path: Path to the PDF file.
        api_key:  Google API key for the ``gemini`` provider. Falls back to
                  the GOOGLE_API_KEY env var. Ignored by the ``hub`` provider.
        provider: ``"hub"`` (default) or ``"gemini"``. Falls back to the
                  INVOICE_OCR_PROVIDER env var, then ``invoice_ocr.provider``
                  in config.json, then ``DEFAULT_PROVIDER``.

    Returns:
        Parsed dict with extracted fields plus ``_raw_response`` and
        ``_file_hash`` keys. Schema is identical across providers.

    Raises:
        RuntimeError: If the chosen backend's SDK is missing, credentials are
                      missing, or the call / JSON parse fails.
        FileNotFoundError: If the PDF does not exist.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    with open(pdf_path, "rb") as fh:
        pdf_bytes = fh.read()
    file_hash = hashlib.md5(pdf_bytes).hexdigest()

    resolved = _resolve_provider(provider)
    if resolved == "hub":
        raw_text = _extract_via_hub(pdf_bytes, pdf_path.name)
    elif resolved == "gemini":
        raw_text = _extract_via_gemini(pdf_bytes, pdf_path.name, api_key)
    else:
        raise RuntimeError(
            f"Unknown invoice OCR provider {resolved!r}. Use 'hub' or 'gemini'."
        )

    log.debug("Raw OCR response for %s: %s", pdf_path.name, raw_text[:500])

    try:
        data = _parse_json_object(raw_text)
    except (json.JSONDecodeError, ValueError) as exc:
        log.error("JSON parse failed for %s: %s\nRaw: %s", pdf_path.name, exc, raw_text)
        raise RuntimeError(
            f"OCR backend returned non-JSON for {pdf_path.name}: {exc}"
        ) from exc

    # Normalise numeric fields — the model sometimes returns strings like "1.234,56"
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
