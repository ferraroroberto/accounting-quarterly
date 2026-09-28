"""Serialize / deserialize tax engine dataclasses for SQLite snapshot storage."""
from __future__ import annotations

import json
from dataclasses import asdict, fields
from typing import Any, Callable, Optional

from src.logger import get_logger
from src.tax_models import (
    Modelo130Result,
    Modelo303Result,
    Modelo347Result,
    Modelo347Row,
    Modelo349Result,
    Modelo349Row,
    OSSCountryRow,
    OSSReturnResult,
)

log = get_logger(__name__)

# Legacy Modelo303Result field names -> current AEAT-box field names. Two
# generations are mapped straight to the current name:
# - pre-e08a3ff9 (#42): box_28_iva_soportado / box_29_base_soportado (swapped);
# - pre-#97: box_NN_* names, where "box_01" was really the 21% row (07/09),
#   "export_base" held non-EU sales (now box 120, not-subject by location) and
#   "box_48_resultado" was 46 with no carry-forward (= 66 at 100% attribution).
# Snapshots persisted before a rename still carry these keys in
# ``payload_json`` and would otherwise raise a TypeError on decode.
_MODELO303_LEGACY_RENAMES: dict[str, str] = {
    "box_28_iva_soportado": "c29_cuota",
    "box_29_base_soportado": "c28_base",
    "box_01_base": "c07_base",
    "box_03_cuota": "c09_cuota",
    "box_59_intracom_entregas": "c59_entregas_intracom",
    "box_28_base_soportado": "c28_base",
    "box_29_cuota_soportado": "c29_cuota",
    "box_46_diferencia": "c46_resultado_regimen_general",
    "box_48_resultado": "c66_atribuible_estado",
    "export_base": "c120_no_sujetas_localizacion",
}


def _int_key_dict(d: dict[Any, Any]) -> dict[int, float]:
    out: dict[int, float] = {}
    for k, v in d.items():
        out[int(k)] = float(v)
    return out


def _tolerant_construct(
    cls: type,
    data: dict[str, Any],
    legacy_renames: dict[str, str] | None = None,
    row_decoder: Optional[Callable[[dict[str, Any]], Any]] = None,
    row_field: str = "rows",
) -> Any:
    """Build a dataclass from stored snapshot data, tolerating legacy/unknown keys.

    Applies ``legacy_renames`` first, then (if ``row_decoder`` is given) decodes
    each dict in ``data[row_field]`` through it, then drops any remaining key
    that isn't a field on ``cls`` (logging a warning) so a future field rename
    on a tax-engine result dataclass can't hard-crash snapshot decoding the way
    ``box_28``/``box_29`` did after commit e08a3ff9.
    """
    data = dict(data)
    for old_key, new_key in (legacy_renames or {}).items():
        if old_key in data:
            data[new_key] = data.pop(old_key)

    if row_decoder is not None:
        data[row_field] = [row_decoder(r) for r in data.get(row_field, [])]

    known_fields = {f.name for f in fields(cls)}
    unknown = sorted(set(data) - known_fields)
    if unknown:
        log.warning(
            "⚠️ Dropping unknown legacy snapshot field(s) %s while decoding %s",
            unknown, cls.__name__,
        )
        for key in unknown:
            data.pop(key)

    return cls(**data)


def _derive_legacy_303_totals(result: Modelo303Result) -> None:
    """Fill the total boxes a pre-#97 snapshot never stored, from the boxes it did.

    Legacy payloads only carried 01/03 (→ 07/09), 28/29 and 46, so 27, 45 and the
    result boxes would otherwise decode as 0 next to non-zero components. No
    carry-forward existed then, so 64 = 66 = 69 = 71 = 46.
    """
    result.c27_total_devengado = round(
        result.c03_cuota + result.c06_cuota + result.c09_cuota
        + result.c11_cuota + result.c13_cuota, 2)
    result.c45_total_deducir = round(result.c29_cuota + result.c31_cuota + result.c37_cuota, 2)
    result.c64_suma_resultados = result.c46_resultado_regimen_general
    result.c66_atribuible_estado = result.c46_resultado_regimen_general
    result.c69_resultado_autoliquidacion = result.c46_resultado_regimen_general
    result.c71_resultado_liquidacion = result.c46_resultado_regimen_general
    result.c46_sin_prorrata = result.c46_resultado_regimen_general


def _decode_oss_row(r: dict[str, Any]) -> OSSCountryRow:
    return OSSCountryRow(**r)


def _decode_347_row(r: dict[str, Any]) -> Modelo347Row:
    qb = r.get("quarter_breakdown") or {}
    if qb and isinstance(next(iter(qb.keys()), None), str):
        qb = _int_key_dict(qb)
    return Modelo347Row(
        counterparty_name=r["counterparty_name"],
        counterparty_nif=r["counterparty_nif"],
        total_operations=float(r["total_operations"]),
        quarter_breakdown=qb,
    )


def _decode_349_row(r: dict[str, Any]) -> Modelo349Row:
    return Modelo349Row(
        buyer_name=r["buyer_name"],
        buyer_vat_id=r["buyer_vat_id"],
        total_amount=float(r["total_amount"]),
    )


def encode_snapshot(model: str, obj: Any) -> str:
    """JSON payload for ``tax_computation_snapshots.payload_json``.

    The ``audit`` list is stored separately in ``tax_audit_log`` and is excluded
    here to keep snapshot payloads lean and decode-compatible.
    """
    data = asdict(obj)
    data.pop("audit", None)
    return json.dumps(data, ensure_ascii=False)


def decode_snapshot(model: str, payload_json: str) -> Any:
    """Restore a computation result object from stored JSON."""
    data = json.loads(payload_json)
    if model == "303":
        result = _tolerant_construct(Modelo303Result, data, _MODELO303_LEGACY_RENAMES)
        if any(k in data for k in _MODELO303_LEGACY_RENAMES):
            _derive_legacy_303_totals(result)
        return result
    if model == "130":
        return _tolerant_construct(Modelo130Result, data)
    if model == "OSS":
        return _tolerant_construct(OSSReturnResult, data, row_decoder=_decode_oss_row)
    if model == "347":
        return _tolerant_construct(Modelo347Result, data, row_decoder=_decode_347_row)
    if model == "349":
        return _tolerant_construct(Modelo349Result, data, row_decoder=_decode_349_row)
    raise ValueError(f"Unknown tax snapshot model: {model}")
