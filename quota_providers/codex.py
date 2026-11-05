# Vendored from rarf/hermes-quota-plugin (MIT); see LICENSE in this directory.
"""Read-only Codex usage for the effective saved Hermes credential pool.

Private grouping keys and tokens live only in this module's local variables.
Account names come only from explicit, sanitized saved labels, not identities.
Usage payloads are normalized by the existing parser; exception bodies are omitted.
"""
from __future__ import annotations

import base64
import json
import math
import queue
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from .base import Deadline, QuotaResult, QuotaWindow, build_unavailable, urlopen_no_redirect

_FETCH_BUDGET_S = 15.0


def display_label(value):
    """Opt-in local display text only, never inferred from credential identity."""
    if not isinstance(value, str):
        return ""
    return "".join(c for c in value if unicodedata.category(c) not in
                   {"Cc", "Cf", "Cs", "Zl", "Zp"}).strip()[:64].rstrip()


def _explicit_display_label(row, claims):
    """Hide core-generated identity labels while retaining user-chosen aliases."""
    value = display_label(row.get("label"))
    if not value:
        return ""
    generated = {display_label(claims.get(key)) for key in ("email", "preferred_username", "upn")}
    return "" if value in generated else value


def _claims(token):
    """Decode a local grouping/expiry hint, not authentication proof."""
    try:
        part = token.split(".")[1]
        value = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _is_expired(value):
    if value is None:
        return False  # Unknown expiry: the endpoint must decide, never refresh.
    try:
        if isinstance(value, str):
            try:
                value = float(value)
            except ValueError:
                stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
                value = stamp.replace(tzinfo=stamp.tzinfo or timezone.utc).timestamp()
        return isinstance(value, bool) or not math.isfinite(value) or value <= time.time()
    except (ValueError, TypeError, OverflowError):
        return True


def _read_store(path):
    """Snapshot only: core auth readers can write corruption backups."""
    try:
        store = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    if not isinstance(store, dict):
        raise ValueError("invalid auth snapshot")
    return store


def _saved_rows():
    # Use canonical roots, but not hermes_cli.auth: even its nominal readers
    # call _load_auth_store, which copies corrupt files. Never load/select a
    # runtime pool (which can seed, rotate, or write). Malformed local files
    # fail closed rather than silently querying a different global account.
    from hermes_constants import get_hermes_home, get_default_hermes_root

    local_home = get_hermes_home()
    global_home = get_default_hermes_root()
    local = _read_store(local_home / "auth.json")

    def section(store, name):
        value = store.get(name)
        return value if isinstance(value, dict) else {}

    rows = section(local, "credential_pool").get("openai-codex")
    if isinstance(rows, list) and rows:
        return rows, True
    try:
        global_store = (_read_store(global_home / "auth.json")
                        if global_home != local_home else {})
    except (OSError, ValueError, UnicodeError):
        global_store = {}  # canonical global fallback is best-effort
    rows = section(global_store, "credential_pool").get("openai-codex")
    if isinstance(rows, list) and rows:
        return rows, True
    state = section(local, "providers").get("openai-codex")
    if not isinstance(state, dict):
        state = section(global_store, "providers").get("openai-codex")
    if isinstance(state, dict):
        tokens = state.get("tokens")
        return [tokens if isinstance(tokens, dict) else state], False
    return [], False


def _credentials():
    """Read effective saved credentials without mutation or runtime selection."""
    rows, pooled = _saved_rows()
    unique = {}
    def priority(row):
        value = row.get("priority") if isinstance(row, dict) else None
        # Missing/invalid priorities follow explicit numeric priorities; ties
        # retain saved order. Do not coerce booleans or numeric-looking strings.
        if isinstance(value, bool):
            return math.inf
        if isinstance(value, int) or (isinstance(value, float) and math.isfinite(value)):
            return value
        return math.inf

    for row in sorted(rows, key=priority) if isinstance(rows, list) else ():
        if not isinstance(row, dict):
            continue
        token = row.get("access_token")
        if not isinstance(token, str) or not token.strip():
            continue
        token = token.strip()
        claims = _claims(token)
        auth = claims.get("https://api.openai.com/auth")
        account = auth.get("chatgpt_account_id") if isinstance(auth, dict) else None
        subject = claims.get("sub")
        # Without both hints, only exact duplicate tokens can be collapsed.
        identity = (
            (account, subject)
            if isinstance(account, str) and isinstance(subject, str) and account and subject
            else token
        )
        saved_account = row.get("account_id")
        if isinstance(saved_account, str) and saved_account.strip():
            account = saved_account.strip()  # legacy explicit header wins, not a grouping key
        expired = _is_expired(claims.get("exp")) or _is_expired(row.get("expires_at"))
        if identity not in unique:
            unique[identity] = (token, account, expired, _explicit_display_label(row, claims), row.get("base_url"), pooled)
        elif unique[identity][2] and not expired:
            # Token freshness must not replace the best-priority display label.
            unique[identity] = (token, account, expired, unique[identity][3], row.get("base_url"), pooled)
    # Codex CLI keeps a separate OAuth cache at ~/.codex/auth.json. Reading it
    # is opt-in because this plugin otherwise only reads Hermes credentials.
    # The API key field is deliberately ignored; only the subscription OAuth
    # access token and account ID are used, in memory, for the existing usage
    # endpoint. No token is written to the quota cache.
    if _codex_cli_auth_enabled():
        cli = _codex_cli_auth_row()
        if cli is not None:
            token, account = cli
            claims = _claims(token)
            auth = claims.get("https://api.openai.com/auth")
            subject = claims.get("sub")
            identity = ((account, subject) if isinstance(account, str) and account and
                        isinstance(subject, str) and subject else token)
            if identity not in unique:
                unique[identity] = (token, account, _is_expired(claims.get("exp")), "",
                                    None, False)
    return list(unique.values())


def _codex_cli_auth_enabled():
    """Opt-in for ~/.codex/auth.json discovery; false unless explicitly enabled."""
    import os
    from .base import opt_in_flag
    value = os.environ.get("HERMES_QUOTA_CODEX_CLI_ENABLED", "").strip().lower()
    if value in {"0", "false", "no", "off"}:
        return False
    if value in {"1", "true", "yes", "on"}:
        return True
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
        entry = (((cfg.get("plugins") or {}).get("entries") or {}).get("quota") or {})
        settings = entry.get("settings") if isinstance(entry, dict) else None
        return opt_in_flag(settings.get("codexCliAuthEnabled")) if isinstance(settings, dict) else False
    except Exception:
        return False


def _codex_cli_auth_row():
    """Read the supported Codex CLI auth.json shape without returning/storing API keys."""
    from pathlib import Path
    import stat
    path = Path.home() / ".codex" / "auth.json"
    try:
        if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    tokens = data.get("tokens") if isinstance(data, dict) else None
    if not isinstance(tokens, dict):
        return None
    token = tokens.get("access_token")
    account = tokens.get("account_id")
    if not isinstance(token, str) or not token.strip():
        return None
    return token.strip(), account.strip() if isinstance(account, str) and account.strip() else None


def _usage_url(base_url, pooled):
    """Reuse core endpoint policy, without its selecting/refreshing resolver."""
    from agent import account_usage

    base_url = base_url if isinstance(base_url, str) else ""
    try:
        from hermes_cli.auth_codex import _codex_pool_route_base_url
    except ImportError:
        _codex_pool_route_base_url = None
    if pooled and _codex_pool_route_base_url is not None:
        base_url = _codex_pool_route_base_url(base_url)
    elif not base_url:
        # Older cores exposed the base helper through auth.py. Explicit pool
        # routes must not be replaced by the ambient default.
        try:
            from hermes_cli.auth_codex import _codex_base_url
        except ImportError:
            from hermes_cli.auth import _codex_base_url
        base_url = _codex_base_url()
    helper = getattr(account_usage, "_codex_backend_urls", None)
    if helper is not None:
        return helper(base_url)[0]
    return account_usage._resolve_codex_usage_url(base_url)


def _fetch_account(credential, parse, deadline):
    token, account, expired, _label, base_url, pooled = credential
    if expired:
        return build_unavailable("openai-codex", "reauth-required")
    if deadline.expired():
        return build_unavailable("openai-codex", "timeout")
    try:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "codex-cli",
        }
        if account:
            headers["ChatGPT-Account-Id"] = account
        claims = _claims(token)
        auth_claims = claims.get("https://api.openai.com/auth")
        if isinstance(auth_claims, dict):
            residency = auth_claims.get("chatgpt_data_residency") or auth_claims.get("chatgpt_compute_residency")
            if isinstance(residency, str) and residency.strip():
                headers["x-openai-internal-codex-residency"] = residency.strip()
        usage_url = _usage_url(base_url, pooled)
        if deadline.expired():
            return build_unavailable("openai-codex", "timeout")
        req = urllib.request.Request(usage_url, headers=headers)
        with urlopen_no_redirect(req, timeout=deadline.slice(15)) as response:
            raw = response.read()
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return build_unavailable("openai-codex", "bad-json")
        return parse(payload)
    except urllib.error.HTTPError as exc:
        status = exc.code
        reason = "reauth-required" if status in (401, 403) else f"http-{status}"
    except (TimeoutError, urllib.error.URLError) as exc:
        reason = ("timeout" if isinstance(exc, TimeoutError) or
                  isinstance(getattr(exc, "reason", None), TimeoutError) else "fetch-error")
    except (ValueError, UnicodeError):
        reason = "bad-json"
    except Exception:
        reason = "fetch-error"
    return build_unavailable("openai-codex", reason)


def _parse_codex_payload(payload: dict) -> QuotaResult:
    def _iso(ts):
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            return None
        try:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            # A timestamp outside the representable range is a schema surprise,
            # not a crash: this runs after the request try/except has closed.
            return None

    def _window(raw: dict, label: str) -> Optional[QuotaWindow]:
        if not isinstance(raw, dict):
            return None
        used = raw.get("used_percent")
        if not isinstance(used, (int, float)) or isinstance(used, bool):
            return None
        try:
            used_percent = float(used)
        except (OverflowError, TypeError, ValueError):
            return None
        if not math.isfinite(used_percent) or not 0.0 <= used_percent <= 100.0:
            return None
        return QuotaWindow(
            label=label,
            used_percent=used_percent,
            reset_at=_iso(raw.get("reset_at")),
        )

    windows: list[QuotaWindow] = []
    # A malformed optional field must not erase healthy quota windows.
    rate_limit = payload.get("rate_limit")
    if not isinstance(rate_limit, dict):
        rate_limit = {}
    duration_labels = {18_000: "Session", 604_800: "Weekly"}
    for key, fallback in (("primary_window", "Session"), ("secondary_window", "Weekly")):
        raw_window = rate_limit.get(key)
        raw_window = raw_window if isinstance(raw_window, dict) else {}
        seconds = raw_window.get("limit_window_seconds")
        label = fallback
        if isinstance(seconds, int) and not isinstance(seconds, bool):
            label = duration_labels.get(seconds, fallback)
        elif isinstance(seconds, float) and math.isfinite(seconds):
            label = duration_labels.get(int(seconds), fallback)
        w = _window(raw_window, label)
        if w is not None:
            windows.append(w)

    # Per-model limits (research-preview models like Codex Spark).
    extras = payload.get("additional_rate_limits")
    for extra in extras if isinstance(extras, list) else ():
        if not isinstance(extra, dict):
            continue
        model_name = str(extra.get("limit_name") or "").strip()
        if not model_name:
            continue
        short = model_name.replace("GPT-", "").replace("-Codex-", " Codex ")
        inner = extra.get("rate_limit")
        if not isinstance(inner, dict):
            continue
        for key, label in (("primary_window", "5h"), ("secondary_window", "Weekly")):
            w = _window(inner.get(key) or {}, f"{short} · {label}")
            if w is not None:
                windows.append(w)

    details: list[str] = []
    reset_credits = payload.get("rate_limit_reset_credits")
    if not isinstance(reset_credits, dict):
        reset_credits = {}
    def _finite_number(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            value = float(value)
            return value if math.isfinite(value) else None
        except (OverflowError, ValueError):
            return None

    banked = _finite_number(reset_credits.get("available_count"))
    if banked is not None and banked >= 1:
        count = int(banked)
        plural = "s" if count != 1 else ""
        details.append(f"You have {count} reset{plural} banked - use /usage reset to activate")
    credits = payload.get("credits")
    if not isinstance(credits, dict):
        credits = {}
    if credits.get("has_credits"):
        balance = _finite_number(credits.get("balance"))
        if balance is not None:
            details.append(f"Credits balance: ${float(balance):.2f}")
        elif credits.get("unlimited"):
            details.append("Credits balance: unlimited")

    plan = str(payload.get("plan_type") or "").strip()
    plan = plan.title() if plan else None
    if not windows and not details:
        return build_unavailable("openai-codex", "no-data")
    return QuotaResult(
        label="openai-codex",
        windows=windows,
        plan=plan,
        unavailable_reason=None,
        details=details,
    )


def _fetch_codex_with_models() -> QuotaResult:
    return fetch_codex_quota(_parse_codex_payload)


def fetch_codex_quota(parse):
    """Collect independent accounts within one shared wall-clock budget."""
    # Late workers only write to their private queue. They cannot mutate the
    # returned/cache snapshot, though socket I/O can outlive collection.
    deadline = Deadline(_FETCH_BUDGET_S)
    # Local snapshots need no separate discovery thread. The outer cache sweep
    # bounds a stalled file read; never start HTTP if discovery spent our budget.
    try:
        credentials = _credentials()
    except Exception:
        return build_unavailable("openai-codex", "fetcher-unavailable")
    if deadline.expired():
        return build_unavailable("openai-codex", "timeout")
    if not credentials:
        return build_unavailable("openai-codex", "no-credentials")
    completed = queue.Queue()

    def worker(index, credential):
        try:
            result = _fetch_account(credential, parse, deadline)
        except Exception:
            result = build_unavailable("openai-codex", "fetch-error")
        completed.put((index, result))

    from contextvars import copy_context
    for index, credential in enumerate(credentials):
        # Endpoint helpers consult the profile context, not just process globals.
        threading.Thread(target=copy_context().run,
                         args=(worker, index, credential), daemon=True).start()
    results = {}
    while len(results) < len(credentials):
        try:
            index, result = completed.get(timeout=deadline.remaining())
        except queue.Empty:
            break
        results[index] = result
    accounts = []
    for index in range(len(credentials)):
        result = results.get(index, build_unavailable("openai-codex", "timeout"))
        label = credentials[index][3]
        result.label = label or f"Account {index + 1}"
        if len(credentials) > 1:
            # /usage reset acts on the runtime-selected account, not this row.
            result.details = [detail.removesuffix(" - use /usage reset to activate")
                              for detail in result.details]
        accounts.append(result)
    if len(accounts) == 1:
        accounts[0].account_label = credentials[0][3] or None
        accounts[0].label = "openai-codex"
        return accounts[0]
    return QuotaResult(
        label="openai-codex",
        accounts=accounts,
        unavailable_reason=None if any(a.has_data() for a in accounts) else "no-data",
    )


from .registry import register as _register  # noqa: E402

_register("openai-codex")(_fetch_codex_with_models)
