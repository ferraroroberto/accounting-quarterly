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

# Pre-e08a3ff9 (#42) Modelo303Result field names -> current AEAT-casilla-matching
# names. Snapshots persisted before that rename still carry these legacy keys in
# ``payload_json`` and would otherwise raise a TypeError on decode.
_MODELO303_LEGACY_RENAMES: dict[str, str] = {
    "box_28_iva_soportado": "box_29_cuota_soportado",
    "box_29_base_soportado": "box_28_base_soportado",
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
        return _tolerant_construct(Modelo303Result, data, _MODELO303_LEGACY_RENAMES)
    if model == "130":
        return _tolerant_construct(Modelo130Result, data)
    if model == "OSS":
        return _tolerant_construct(OSSReturnResult, data, row_decoder=_decode_oss_row)
    if model == "347":
        return _tolerant_construct(Modelo347Result, data, row_decoder=_decode_347_row)
    if model == "349":
        return _tolerant_construct(Modelo349Result, data, row_decoder=_decode_349_row)
    raise ValueError(f"Unknown tax snapshot model: {model}")
