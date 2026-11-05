# Vendored from rarf/hermes-quota-plugin (MIT); see LICENSE in this directory.
"""Provider-quota fetcher registry (quota plugin, standalone)."""

from __future__ import annotations

import functools
from typing import Callable, Optional

from .base import QuotaResult, build_unavailable

# Provider id -> fetcher callable.  Order here is display priority.
PROVIDER_FETCHERS: dict[str, Callable[[], object]] = {}


def register(provider_id: str):
    """Register a fetcher, enforcing the fail-open contract at the seam.

    Every failure path returns ``build_unavailable(...)``. Guarding here keeps
    provider error handling readable and preserves the ``fetch-error`` reason.
    """

    def _wrap(fn: Callable[[], object]):
        @functools.wraps(fn)
        def _fail_open() -> QuotaResult:
            try:
                return fn()  # type: ignore[return-value]
            except Exception:  # noqa: BLE001 - fail-open by contract
                return build_unavailable(provider_id, "fetch-error")

        PROVIDER_FETCHERS[provider_id] = _fail_open
        return fn

    return _wrap


def get_fetcher(provider_id: str) -> Optional[Callable[[], object]]:
    return PROVIDER_FETCHERS.get(provider_id)
