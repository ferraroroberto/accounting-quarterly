"""Pooled, keep-alive outbound HTTP client for long-lived processes (#192).

A bare ``requests.get``/``requests.post`` inside a poll loop, health check, or
per-item fan-out opens a fresh TCP connection every call. On Windows each
closed connection parks its ephemeral port in ``TIME_WAIT`` for ~120s against
a 16,384-entry range, so a busy poller is enough to exhaust the range and
stall every process on the machine from opening any outbound socket for
minutes (root cause: ``fleet-config#440``). ``app-launcher#605`` measured 145
such sockets to one sibling process's port at a single sample, dropping to 0
after adopting this pattern.

Vendor-verbatim: consuming apps copy this file byte-identical into their own
`src/` (like `app/tray/single_instance.py`, `tests/e2e/_e2e_live_guard.py`) —
the pool size and the target URL are call-site arguments, so the copy never
forks.
"""

from __future__ import annotations

import logging
from typing import Any

import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger(__name__)

_DEFAULT_POOL_SIZE = 20


def build_session(pool_size: int = _DEFAULT_POOL_SIZE) -> requests.Session:
    """A `requests.Session` with a sized, keep-alive `HTTPAdapter` mounted on
    both schemes.

    Build once per process (e.g. assign to a module-level constant at import)
    and never reconfigure per call — `requests.Session` is not thread-safe for
    *configuration* mutation, though plain request dispatch on a shared
    `HTTPAdapter` pool is fine across several threads (e.g. `asyncio.to_thread`
    workers) sharing it concurrently. Size `pool_size` deliberately for
    whatever fan-out this process actually does; the `requests` default of 10
    undersizes a poller that fans out across many endpoints/sessions at once.
    """
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


SESSION = build_session()


#: Methods a repeated request cannot make worse (RFC 9110 "idempotent"). POST and PATCH are not.
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE", "TRACE"})


def pooled_request(
    method: str,
    url: str,
    *,
    timeout: float,
    session: requests.Session = SESSION,
    **kwargs: Any,
) -> requests.Response:
    """One call over `session`'s keep-alive pool, retried once on a dropped
    connection when the method is idempotent.

    A pooled connection can go stale between calls — most commonly the peer
    process restarting and closing sockets it still held open. urllib3
    usually detects and silently reopens a dead pooled socket before sending,
    but the rarer race where the peer closes mid-send still surfaces as
    `requests.exceptions.ConnectionError`. That error cannot tell "closed
    before the request arrived" from "processed it, then dropped the reply"
    (`RemoteDisconnected` is wrapped into the same class), so only an
    idempotent method (GET, HEAD, OPTIONS, PUT, DELETE, TRACE) is resent; a
    POST or PATCH surfaces the error to the caller, who knows whether a
    duplicate is harmless. A `ConnectTimeout` is never retried: the peer is
    unreachable, and a second attempt only doubles the wait.
    """
    try:
        return session.request(method, url, timeout=timeout, **kwargs)
    except requests.exceptions.ConnectTimeout:
        raise
    except requests.exceptions.ConnectionError:
        if method.upper() not in _IDEMPOTENT_METHODS:
            raise
        return session.request(method, url, timeout=timeout, **kwargs)
