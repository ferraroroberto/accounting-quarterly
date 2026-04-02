from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import requests

from src.config import load_config
from src.logger import get_logger

log = get_logger(__name__)


class AccountingAPIError(RuntimeError):
    pass


@dataclass(frozen=True)
class AccountingAPIConfig:
    base_url: str
    subscription_key: str
    token: Optional[str]
    user: Optional[str]
    password: Optional[str]


def _get_env(name: str) -> Optional[str]:
    v = os.getenv(name)
    return v if v and v.strip() else None


def load_accounting_api_config() -> AccountingAPIConfig:
    cfg = load_config()
    api_cfg = cfg.get("accounting_api", {}) or {}

    base_url = (_get_env("ACCOUNTING_BASE_URL") or "").rstrip("/")
    if not base_url:
        raise AccountingAPIError("ACCOUNTING_BASE_URL not set in environment / .env file")

    subscription_key = _get_env("ACCOUNTING_SUBSCRIPTION_KEY")
    if not subscription_key:
        raise AccountingAPIError("ACCOUNTING_SUBSCRIPTION_KEY not set in environment / .env file")

    return AccountingAPIConfig(
        base_url=base_url,
        subscription_key=subscription_key,
        token=_get_env("ACCOUNTING_TOKEN"),
        user=_get_env("ACCOUNTING_USER"),
        password=_get_env("ACCOUNTING_PASSWORD"),
    )


class AccountingAPIClient:
    def __init__(self, cfg: AccountingAPIConfig, timeout_s: int = 60):
        self.cfg = cfg
        self.timeout_s = timeout_s

    def _headers(self, token: Optional[str] = None) -> dict[str, str]:
        headers = {
            "SUBSCRIPTION_KEY": self.cfg.subscription_key,
        }
        tok = token or self.cfg.token
        if tok:
            headers["token"] = tok
        return headers

    def _request(self, method: str, path: str, *, headers: Optional[dict[str, str]] = None, **kwargs) -> Any:
        url = f"{self.cfg.base_url}{path}"
        try:
            resp = requests.request(
                method=method,
                url=url,
                headers=headers or self._headers(),
                timeout=self.timeout_s,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise AccountingAPIError(f"Accounting API request failed: {exc}") from exc

        if resp.status_code >= 400:
            body = resp.text[:2000]
            raise AccountingAPIError(f"Accounting API {method} {path} failed ({resp.status_code}): {body}")

        ctype = (resp.headers.get("content-type") or "").lower()
        if "application/json" in ctype:
            return resp.json()
        return resp.content

    def get_token(self, *, cif: Optional[str] = None, code: Optional[str] = None) -> str:
        if not self.cfg.user or not self.cfg.password:
            raise AccountingAPIError("ACCOUNTING_USER / ACCOUNTING_PASSWORD not set (needed for /api-global/v1/token)")

        params: dict[str, str] = {}
        if cif:
            params["cif"] = cif
        if code:
            params["code"] = code

        headers = {
            "SUBSCRIPTION_KEY": self.cfg.subscription_key,
            "USER": self.cfg.user,
            "PASSWORD": self.cfg.password,
        }

        data = self._request("GET", "/api-global/v1/token", headers=headers, params=params)
        token = (((data or {}).get("data") or {}).get("token")) if isinstance(data, dict) else None
        if not token:
            raise AccountingAPIError(f"Unexpected token response: {data}")
        return token

    def test_connection(self, *, token: Optional[str] = None) -> tuple[bool, str]:
        try:
            self._request("GET", "/api-global/v1/getPrograms", headers=self._headers(token))
            return True, "Connected: /getPrograms OK."
        except Exception as exc:
            return False, str(exc)

    def get_companies(self, *, token: str, company_id: Optional[str] = None, cif: Optional[str] = None) -> Any:
        params: dict[str, str] = {}
        if company_id:
            params["company_id"] = company_id
        if cif:
            params["cif"] = cif
        return self._request(
            "GET",
            "/api-global/v1/getCompanies",
            headers=self._headers(token),
            params=params if params else None,
        )

    def upload_document(
        self,
        *,
        token: str,
        doc_type: str,
        file_path: str,
        date: datetime,
        year: int,
        overwrite: int = 0,
        notified: int = 0,
        observation: Optional[str] = None,
    ) -> dict[str, Any]:
        with open(file_path, "rb") as f:
            files = {"file1": (os.path.basename(file_path), f, "application/pdf")}
            data = {
                "type": doc_type,
                "date": date.strftime("%Y-%m-%d %H:%M:%S"),
                "year": str(int(year)),
                "overwrite": str(int(overwrite)),
                "notified": str(int(notified)),
            }
            if observation:
                data["observation"] = observation

            res = self._request(
                "POST",
                "/api-global/v1/documents/postUploadDocuments",
                headers=self._headers(token),
                files=files,
                data=data,
            )

        if not isinstance(res, dict):
            raise AccountingAPIError(f"Unexpected upload response: {type(res)}")
        return res

    def list_directory(
        self,
        *,
        token: str,
        company_id: str,
        doc_type: Optional[str],
        start_date: str,
        end_date: str,
    ) -> Any:
        params: dict[str, str] = {
            "companyId": company_id,
            "startDate": start_date,
            "endDate": end_date,
        }
        if doc_type:
            params["type"] = doc_type
        return self._request(
            "GET",
            "/api-global/v1/documents/getDirectory",
            headers=self._headers(token),
            params=params,
        )

    def download_document(self, *, token: str, document_id: str) -> bytes:
        content = self._request(
            "GET",
            "/api-global/v1/documents/getDocument",
            headers=self._headers(token),
            params={"document_id": document_id},
        )
        if isinstance(content, (bytes, bytearray)):
            return bytes(content)
        raise AccountingAPIError("Unexpected download response (expected bytes).")

