# Vendored from rarf/hermes-quota-plugin (MIT); see LICENSE in this directory.
"""Claude Code (Anthropic OAuth) quota fetcher — single-account trimmed copy.

    GET https://api.anthropic.com/api/oauth/usage
    -> {"five_hour": {"utilization": 12.0, "resets_at": "..."},
        "seven_day": {...}, "limits": [{"kind": "weekly_scoped", "percent": 3, ...}]}

Credentials: ``~/.claude/.credentials.json`` (``claudeAiOauth.accessToken``,
falling back to a top-level ``accessToken``). Read-only; the token is never
logged, cached, or placed in a reason string.

Fail-open contract: the endpoint aggressively returns HTTP 429 when polled, so
EVERY failure (HTTP error, missing credentials, malformed payload) returns
``build_unavailable("claude", reason)``. A failed fetch never marks the account
exhausted and never creates a cooldown; the cache keeps the last good entry.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import urllib.error
import urllib.request
from typing import Any, Optional

from .base import QuotaResult, QuotaWindow, build_unavailable, urlopen_no_redirect

_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_TIMEOUT_S = 15.0
_MAX_BYTES = 65536


def _credentials_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")


def _read_token() -> Optional[str]:
    try:
        with open(_credentials_path(), "r", encoding="utf-8") as fh:
            data = json.loads(fh.read(_MAX_BYTES + 1))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    oauth = data.get("claudeAiOauth")
    token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    if not isinstance(token, str) or not token.strip():
        token = data.get("accessToken")
    return token.strip() if isinstance(token, str) and token.strip() else None


def _token_fingerprint(token: str) -> str:
    """Short one-way fingerprint: a stable account id without exposing the token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _request_usage(token: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    request = urllib.request.Request(
        _USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "anthropic-beta": "oauth-2025-04-20",
        },
        method="GET",
    )
    try:
        with urlopen_no_redirect(request, timeout=_TIMEOUT_S) as resp:
            body = resp.read(_MAX_BYTES + 1)
        if len(body) > _MAX_BYTES:
            return None, "bad-json"
        payload = json.loads(body)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return None, "auth-failed"
        return None, f"http-{exc.code}"
    except ValueError:
        return None, "bad-json"
    except Exception as exc:  # noqa: BLE001 - fail-open
        return None, f"fetch-error:{type(exc).__name__}"
    if not isinstance(payload, dict):
        return None, "bad-json"
    return payload, None


def _reset(value: Any) -> Optional[str]:
    if isinstance(value, str) and value:
        return value[:-1] + "+00:00" if value.endswith("Z") else value
    return None


def _percent(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        used = float(value)
    except (TypeError, ValueError):
        return None
    return used if math.isfinite(used) else None


def parse_scoped_limits(payload: dict[str, Any]) -> list[QuotaWindow]:
    """Model-scoped weekly limits from the newer ``limits`` list."""
    limits = payload.get("limits")
    windows: list[QuotaWindow] = []
    for entry in limits if isinstance(limits, list) else ():
        if not isinstance(entry, dict) or entry.get("kind") not in {"session", "weekly_all", "weekly_scoped"}:
            continue
        percent = _percent(entry.get("percent"))
        if percent is None:
            continue
        kind = entry["kind"]
        if kind == "session":
            label = "Current session"
        elif kind == "weekly_all":
            label = "Current week"
        else:
            scope = entry.get("scope") if isinstance(entry.get("scope"), dict) else {}
            model = scope.get("model") if isinstance(scope.get("model"), dict) else {}
            name = model.get("display_name") or model.get("id") or scope.get("surface")
            if not isinstance(name, str) or not name.strip():
                continue
            label = f"{name.strip()} week"
        windows.append(QuotaWindow(label=label, used_percent=max(0.0, min(100.0, percent)),
                                   reset_at=_reset(entry.get("resets_at"))))
    return windows


def parse_usage(payload: dict[str, Any]) -> list[QuotaWindow]:
    windows: list[QuotaWindow] = []
    seen: set[str] = set()
    for key, label in (("five_hour", "Current session"), ("seven_day", "Current week"),
                       ("seven_day_opus", "Opus week"), ("seven_day_sonnet", "Sonnet week")):
        window = payload.get(key)
        used = _percent(window.get("utilization")) if isinstance(window, dict) else None
        if used is None:
            continue
        if 0.0 < used < 1.0:  # fraction form; a bare 1.0 stays 1%
            used *= 100
        seen.add(label)
        windows.append(QuotaWindow(label=label, used_percent=max(0.0, min(100.0, used)),
                                   reset_at=_reset(window.get("resets_at"))))
    for window in parse_scoped_limits(payload):
        if window.label not in seen:
            seen.add(window.label)
            windows.append(window)
    return windows


from .registry import register as _register  # noqa: E402


@_register("claude")
def fetch_claude_quota() -> QuotaResult:
    token = _read_token()
    if not token:
        return build_unavailable("claude", "no-credentials")
    payload, reason = _request_usage(token)
    if payload is None:
        return build_unavailable("claude", reason or "no-data")
    windows = parse_usage(payload)
    if not windows:
        return build_unavailable("claude", "no-data")
    return QuotaResult(label="claude", windows=windows, plan=None, unavailable_reason=None,
                       account_label=_token_fingerprint(token))
