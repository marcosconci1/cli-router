# Vendored from rarf/hermes-quota-plugin (MIT); see LICENSE in this directory.
"""Trimmed, standard-library quota fetchers for Codex, Kimi, and Claude."""

from .base import QuotaResult, QuotaWindow, build_unavailable
from .registry import PROVIDER_FETCHERS, get_fetcher, register
from . import claude as _claude
from . import codex as _codex
from . import kimi as _kimi

try:  # Optional provider kept outside the public distribution.
    from . import antigravity as _antigravity
except ModuleNotFoundError as exc:  # Module absent: AGY quota reads as unknown.
    if exc.name != "quota_providers.antigravity":
        raise  # A broken dependency inside some other module is a real error.

__all__ = [
    "PROVIDER_FETCHERS",
    "QuotaResult",
    "QuotaWindow",
    "build_unavailable",
    "get_fetcher",
    "register",
]
