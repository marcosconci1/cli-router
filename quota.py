# Cache schema credit: rarf/hermes-quota-plugin (MIT).
"""Profile-local quota cache access with vendored refresh and rarf fallback."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

try:
    from . import hermes_adapter, quota_providers
    from .quota_providers.base import QuotaAccount, QuotaResult
except ImportError:
    import hermes_adapter
    import quota_providers
    from quota_providers.base import QuotaAccount, QuotaResult


class QuotaSource(Protocol):
    def read(self) -> dict[str, Any]:
        ...

    def age_seconds(self) -> float | None:
        ...

    def refresh(self) -> None:
        ...


def _environment_home() -> Path:
    home = os.environ.get("HERMES_HOME")
    if home:
        return Path(home).resolve()
    raise RuntimeError("HERMES_HOME is not set and hermes_constants is unavailable")


def _hermes_home() -> Path:
    plugin_dir = Path(__file__).resolve().parent
    profile_home = plugin_dir.parent.parent
    if plugin_dir.parent.name == "plugins" and profile_home.parent.name == "profiles":
        try:
            from hermes_constants import get_default_hermes_root
        except ImportError:
            return _environment_home()
        return Path(get_default_hermes_root()).resolve() / "profiles" / profile_home.name
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        return _environment_home()
    return Path(get_hermes_home()).resolve()


def _optional_rarf_plugin(home: Path | None = None) -> Any | None:
    plugin = (home or _hermes_home()) / "plugins" / "hermes-quota-plugin"
    if not (plugin / "__init__.py").is_file():
        return None
    plugin = plugin.resolve()
    for name, module in tuple(sys.modules.items()):
        if name.endswith("quota_cache") and all(hasattr(module, attr) for attr in
                ("read_quota_cache", "quota_cache_age_seconds", "refresh_quota_cache")):
            module_file = getattr(module, "__file__", None)
            if module_file is not None:
                try:
                    if Path(module_file).resolve().is_relative_to(plugin):
                        return module
                except (OSError, RuntimeError, TypeError, ValueError):
                    pass
    package_name = "_hermes_code_quota_plugin"
    package = sys.modules.get(package_name)
    if package is None:
        spec = importlib.util.spec_from_file_location(
            package_name, plugin / "__init__.py", submodule_search_locations=[str(plugin)])
        if spec is None or spec.loader is None:
            raise RuntimeError("profile quota plugin could not be loaded")
        package = importlib.util.module_from_spec(spec)
        sys.modules[package_name] = package
        spec.loader.exec_module(package)
    module = sys.modules.get(f"{package_name}.quota_cache")
    if module is None:
        raise RuntimeError("profile quota plugin cache API is unavailable")
    return module


def rarf_plugin() -> Any:
    module = _optional_rarf_plugin()
    if module is None:
        raise RuntimeError("profile quota plugin is not installed")
    return module


def _window_record(window: Any) -> dict[str, Any]:
    item = {"label": window.label, "used_percent": window.used_percent, "reset_at": window.reset_at}
    for key in ("remaining", "limit", "unit"):
        value = getattr(window, key)
        if value is not None:
            item[key] = value
    return item


def _result_to_record(result: QuotaResult) -> dict[str, Any]:
    record: dict[str, Any] = {
        "label": result.label,
        "plan": result.plan,
        "unavailable_reason": result.unavailable_reason,
        "details": list(result.details or []),
        "account_balances": [
            {"currency": balance.currency, "total_balance": balance.total_balance,
             "granted_balance": balance.granted_balance, "topped_up_balance": balance.topped_up_balance}
            for balance in result.account_balances
        ],
        "api_calls_available": result.api_calls_available,
        "windows": [_window_record(window) for window in result.windows],
    }
    if result.account_label:
        record["account_label"] = result.account_label
    if result.accounts:
        record["accounts"] = [
            ({
                "id": account.id,
                "label": account.label,
                "plan": account.plan,
                "unavailable_reason": account.unavailable_reason,
                "details": list(account.details or []),
                "windows": [_window_record(window) for window in account.windows],
            } if isinstance(account, QuotaAccount) else _result_to_record(account))
            for account in result.accounts
        ]
    return record


def _unavailable_record(provider_id: str, reason: str) -> dict[str, Any]:
    return {"label": provider_id, "plan": None, "unavailable_reason": reason,
            "details": [], "windows": []}


# Router-owned providers (no rarf equivalent keyed for the CLI) live in a sidecar file:
# rarf rewrites quota_cache.json wholesale and would drop them. Their endpoints also
# rate-limit hard (Anthropic usage 429s when polled), so each refreshes on its own
# minimum interval and a failed fetch keeps the last good entry.
_ROUTER_OWNED_INTERVAL_S = {"claude": 300.0}
_SIDECAR_NAME = "cli_router_quota.json"


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class FileCacheQuotaSource:
    def __init__(self, home: str | os.PathLike[str] | None = None):
        self._home = Path(home).resolve() if home is not None else None

    def _cache_path(self) -> Path:
        return (self._home or _hermes_home()) / "quota_cache.json"

    def read(self) -> dict[str, Any]:
        path = self._cache_path()
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise RuntimeError(f"quota cache file is missing: {path}") from exc
        except OSError as exc:
            raise RuntimeError(f"quota cache file could not be read: {path}") from exc
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise RuntimeError("quota cache response is invalid JSON") from exc
        providers = payload.get("providers") if isinstance(payload, dict) else None
        if not isinstance(providers, dict):
            raise RuntimeError("quota cache response is invalid: providers must be an object")
        providers = dict(providers)
        for provider_id, entry in self._read_sidecar().items():
            providers[provider_id] = entry["record"]
        return {"fetched_at": payload.get("fetched_at"), "providers": providers}

    def _sidecar_path(self) -> Path:
        return (self._home or _hermes_home()) / _SIDECAR_NAME

    def _read_sidecar(self) -> dict[str, dict[str, Any]]:
        """Router-owned provider entries; a missing or damaged sidecar is simply empty."""
        try:
            payload = json.loads(self._sidecar_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        entries = payload.get("providers") if isinstance(payload, dict) else None
        return {provider_id: entry for provider_id, entry in (entries or {}).items()
                if provider_id in _ROUTER_OWNED_INTERVAL_S and isinstance(entry, dict)
                and isinstance(entry.get("record"), dict)} if isinstance(entries, dict) else {}

    def _refresh_router_owned(self, provider_id: str, fetcher: Any,
                              sidecar: dict[str, dict[str, Any]]) -> bool:
        """Refresh one sidecar provider when its interval has elapsed. Returns True if written."""
        previous = sidecar.get(provider_id) or {}
        try:
            attempted = float(previous.get("attempted_at"))
        except (TypeError, ValueError):
            attempted = None
        now = datetime.now(timezone.utc).timestamp()
        if attempted is not None and 0 <= now - attempted < _ROUTER_OWNED_INTERVAL_S[provider_id]:
            return False
        try:
            result = fetcher()
            if not isinstance(result, QuotaResult):
                raise TypeError("quota fetcher did not return QuotaResult")
            record = _result_to_record(result)
        except Exception:
            record = _unavailable_record(provider_id, "fetch-error")
        entry = {"attempted_at": now, "record": record, "fetched_at": now}
        last = previous.get("record")
        if record.get("unavailable_reason") and isinstance(last, dict) and last.get("windows"):
            # Transient failures (e.g. Anthropic's aggressive usage 429) keep the last good entry.
            entry = {**previous, "attempted_at": now}
        sidecar[provider_id] = entry
        return True

    def age_seconds(self) -> float | None:
        fetched_at = self.read().get("fetched_at")
        try:
            stamp = datetime.fromisoformat(fetched_at)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds()
        except (TypeError, ValueError):
            return None

    def _refresh_vendored(self) -> bool:
        try:
            existing = self.read()
        except RuntimeError:
            existing = {"fetched_at": None, "providers": {}}
        providers = {provider_id: record for provider_id, record in existing["providers"].items()
                     if provider_id not in _ROUTER_OWNED_INTERVAL_S}
        sidecar = self._read_sidecar()
        sidecar_changed = False
        fallback_needed = False
        for provider_id, fetcher in quota_providers.PROVIDER_FETCHERS.items():
            if provider_id in _ROUTER_OWNED_INTERVAL_S:
                sidecar_changed = self._refresh_router_owned(provider_id, fetcher, sidecar) or sidecar_changed
                continue
            try:
                result = fetcher()
                if not isinstance(result, QuotaResult):
                    raise TypeError("quota fetcher did not return QuotaResult")
                record = _result_to_record(result)
            except Exception:
                record = _unavailable_record(provider_id, "fetch-error")
            fallback_needed = fallback_needed or record.get("unavailable_reason") == "fetch-error"
            providers[provider_id] = record
        if sidecar_changed:
            _write_json_atomic(self._sidecar_path(), {"providers": sidecar})
        _write_json_atomic(self._cache_path(),
                           {"fetched_at": datetime.now(timezone.utc).isoformat(), "providers": providers})
        return fallback_needed

    def refresh(self) -> None:
        try:
            fallback_needed = self._refresh_vendored()
        except Exception:
            fallback_needed = True
        if fallback_needed:
            try:
                module = _optional_rarf_plugin(self._home)
                if module is not None:
                    module.refresh_quota_cache()
            except Exception:
                pass


class ManualQuotaSource:
    """Manual quota source reading limits from plugin settings key quota.manual."""

    def __init__(self) -> None:
        pass

    def read(self) -> dict[str, Any]:
        if not hermes_adapter.has_context():
            raise RuntimeError("ManualQuotaSource requires Hermes plugin context; standalone reads are not supported")
        try:
            cfg = hermes_adapter.get_config("quota.manual", {})
        except Exception as exc:
            raise RuntimeError("failed to read plugin configuration for manual quota limits") from exc
        if not isinstance(cfg, dict):
            cfg = {}
        raw_manual = cfg
        if any(key in cfg for key in ("quota.manual", "quota", "settings")):
            # Support both {"quota.manual": ...} and {"quota": {"manual": ...}} in the value.
            raw_manual = cfg.get("quota.manual")
            if raw_manual is None and isinstance(cfg.get("quota"), dict):
                raw_manual = cfg["quota"].get("manual")
            if raw_manual is None and isinstance(cfg.get("settings"), dict):
                settings = cfg["settings"]
                raw_manual = settings.get("quota.manual")
                if raw_manual is None and isinstance(settings.get("quota"), dict):
                    raw_manual = settings["quota"].get("manual")

        providers: dict[str, Any] = {}
        if isinstance(raw_manual, dict):
            for provider_id, data in raw_manual.items():
                if not isinstance(data, dict):
                    continue
                windows = []
                for w in data.get("windows", []):
                    if not isinstance(w, dict):
                        continue
                    window_item = {
                        "label": w.get("label", "manual"),
                        "reset_at": w.get("reset_at"),
                        "used_percent": w.get("used_percent", 0.0),
                    }
                    for extra in ("remaining", "limit", "unit"):
                        if extra in w:
                            window_item[extra] = w[extra]
                    windows.append(window_item)
                providers[provider_id] = {
                    "label": data.get("label", provider_id),
                    "plan": data.get("plan"),
                    "unavailable_reason": data.get("unavailable_reason"),
                    "details": list(data.get("details") or []),
                    "windows": windows,
                }
        return {"fetched_at": datetime.now(timezone.utc).isoformat(), "providers": providers}

    def age_seconds(self) -> float | None:
        return 0.0

    def refresh(self) -> None:
        pass


def default_source(home: str | os.PathLike[str] | None = None) -> QuotaSource:
    file_source = FileCacheQuotaSource(home)
    cache_path = file_source._cache_path()
    try:
        cache_exists = cache_path.is_file()
    except Exception:
        cache_exists = False

    if cache_exists:
        return file_source

    # Cache is missing: check if manual quota source can be used inside Hermes
    if hermes_adapter.has_context():
        try:
            manual_source = ManualQuotaSource()
            manual_data = manual_source.read()
            if manual_data.get("providers"):
                return manual_source
        except Exception:
            pass

    # Cache missing or refresh produces no data; try refresh on file_source if possible
    # or return manual_source if inside Hermes, otherwise file_source
    if hermes_adapter.has_context():
        return ManualQuotaSource()
    return file_source
