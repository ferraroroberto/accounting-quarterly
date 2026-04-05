"""Serialize / deserialize tax engine dataclasses for SQLite snapshot storage."""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

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


def _int_key_dict(d: dict[Any, Any]) -> dict[int, float]:
    out: dict[int, float] = {}
    for k, v in d.items():
        out[int(k)] = float(v)
    return out


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
        return Modelo303Result(**data)
    if model == "130":
        return Modelo130Result(**data)
    if model == "OSS":
        rows = [OSSCountryRow(**r) for r in data.get("rows", [])]
        return OSSReturnResult(
            year=data["year"],
            quarter=data["quarter"],
            rows=rows,
            total_base=data["total_base"],
            total_vat=data["total_vat"],
            total_transactions=data["total_transactions"],
        )
    if model == "347":
        rows_out: list[Modelo347Row] = []
        for r in data.get("rows", []):
            qb = r.get("quarter_breakdown") or {}
            if qb and isinstance(next(iter(qb.keys()), None), str):
                qb = _int_key_dict(qb)
            rows_out.append(
                Modelo347Row(
                    counterparty_name=r["counterparty_name"],
                    counterparty_nif=r["counterparty_nif"],
                    total_operations=float(r["total_operations"]),
                    quarter_breakdown=qb,
                )
            )
        return Modelo347Result(
            year=data["year"],
            rows=rows_out,
            threshold=float(data.get("threshold", 3005.06)),
        )
    if model == "349":
        rows_out: list[Modelo349Row] = []
        for r in data.get("rows", []):
            qb = r.get("quarter_breakdown") or {}
            if qb and isinstance(next(iter(qb.keys()), None), str):
                qb = _int_key_dict(qb)
            rows_out.append(
                Modelo349Row(
                    buyer_name=r["buyer_name"],
                    buyer_vat_id=r["buyer_vat_id"],
                    total_amount=float(r["total_amount"]),
                    quarter_breakdown=qb,
                )
            )
        return Modelo349Result(
            year=data["year"],
            quarter=data["quarter"],
            rows=rows_out,
            total=float(data.get("total", 0.0)),
            notes=data.get("notes", ""),
        )
    raise ValueError(f"Unknown tax snapshot model: {model}")
