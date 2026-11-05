"""Exact-model routing evidence, with Akita's 2026-10-28 article primary.

Source: https://akitaonrails.com/2026/10/28/llm-benchmark-v4-gemini-4-argon-gpt-sol-6-1-terra-kimi-k3-1-pro-claude-sonnet-5-5/
(see benchmarks/README.md). Published
engineering breadth indexes retain their 90% coverage label; v4 is a separate
security-vigilance surface. OpenRouter AA indexes supply quality only for models
without Akita results, and live prices remain available independently. Campaign
cost, wall time, per-token pricing and subscription quota are not interchangeable.
No predecessor imputation and no average across unrelated benchmark surfaces.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable

TTL_SECONDS = 6 * 3600

# Exact-model/harness observations derived from the source article. These are
# campaign costs, not per-token prices or measured subscription quota burn.
AKITA_SOURCE = "https://akitaonrails.com/2026/10/28/llm-benchmark-v4-gemini-4-argon-gpt-sol-6-1-terra-kimi-k3-1-pro-claude-sonnet-5-5/"
AKITA_V4 = {
    "gpt-6-luna": {"score": 95.5, "run_cost_usd": 0.84, "wall_minutes": 122},
    "gpt-5.6-terra": {"score": 100.0, "run_cost_usd": 9.52, "wall_minutes": 80},
    "gpt-6.1-sol": {"score": 97.0, "run_cost_usd": 8.12, "wall_minutes": 65},
    "gpt-6.1-terra": {"score": 96.8, "run_cost_usd": 1.69, "wall_minutes": 21},
    "claude-opus-5-5": {"score": 100.0, "run_cost_usd": 16.63, "wall_minutes": 64},
    "claude-sonnet-5-5": {"score": 88.0, "run_cost_usd": 3.90, "wall_minutes": 27},
    "kimi-code/k3": {"score": 85.0, "run_cost_usd": 13.79, "wall_minutes": 148},
    "kimi-code/k3.1-pro": {"score": 92.11, "run_cost_usd": 12.67, "wall_minutes": 110},
    "gemini-4-argon": {"score": 100.0, "run_cost_usd": 0.0, "wall_minutes": 114,
                       "cost_basis": "oauth_marginal_not_quota", "tested_effort": "high"},
    "gemini-3.8-flash": {"score": 89.5, "run_cost_usd": 0.0, "wall_minutes": 120,
                         "cost_basis": "oauth_marginal_not_quota"},
    "google/gemini-3.8-flash": {"score": 90.5, "run_cost_usd": 15.98, "wall_minutes": 97,
                                "cost_basis": "api_campaign"},
}
AKITA_ENGINEERING = {
    "gemini-4-argon": {"score": 96.6, "coverage_percent": 90, "swe_pro_v2": 99.22, "terminal_4": 57.58},
    "gpt-6.1-sol": {"score": 93.2, "coverage_percent": 90, "swe_pro_v2": 96.11, "terminal_4": 55.05},
    "claude-sonnet-5-5": {"score": 94.1, "coverage_percent": 90, "swe_pro_v2": 95.79, "terminal_4": 64.14},
    "gpt-5.6-terra": {"score": 72.5, "coverage_percent": 90, "swe_pro_v2": 92.37, "terminal_4": 22.73},
    "gemini-3.8-flash": {"score": 67.9, "coverage_percent": 90, "swe_pro_v2": 94.86, "terminal_4": 19.70},
    "kimi-code/k3": {"score": 65.0, "coverage_percent": 90, "swe_pro_v2": 97.70, "terminal_4": 17.17},
}

AKITA_PARTIAL = {
    "claude-opus-5-5": {"terminal_4": 65.15, "missing": ["swe_pro_v2"], "engineering_index": None},
    "kimi-code/k3.1-pro": {"terminal_4": 63.64, "missing": ["swe_pro_v2"], "engineering_index": None},
    "gpt-6.1-terra": {"terminal_4": 49.49, "missing": ["swe_pro_v2"], "engineering_index": None},
    "claude-haiku-5-5": {"swe_pro_v2": 87.23, "terminal_4": 36.36,
                         "missing": ["akita_v4"], "engineering_index": None},
}

# Route model -> OpenRouter catalog id (free OpenCode routes already use catalog ids).
OPENROUTER_IDS = {
    "gpt-6-luna": "openai/gpt-6-luna",
    "gpt-6.1-sol": "openai/gpt-6.1-sol",
    "gpt-6.1-terra": "openai/gpt-6.1-terra",
    "gpt-5.6-terra": "openai/gpt-5.6-terra",
    "claude-opus-5-5": "anthropic/claude-opus-5.5",
    "claude-sonnet-5-5": "anthropic/claude-sonnet-5.5",
    "claude-haiku-5-5": "anthropic/claude-haiku-5.5",
    "kimi-code/k3": "moonshotai/kimi-k3",
    "kimi-code/k3.1-pro": "moonshotai/kimi-k3.1-pro",
    "gemini-3.8-flash": "google/gemini-3.8-flash",
    "gemini-4-argon": "google/gemini-4-argon",
}

OPERATOR_NOTES = {
    "kimi-code/kimi-for-coding": ("K2.8 Preview: Kimi Code only, no exact result in either source; "
                                  "owner rates it very good for coding. Do not substitute K2.7 or K3 data."),
    "claude-haiku-5-5": ("No Akita v4 result; use exact OpenRouter evidence when available. "
                          "The article lists SWE Pro V2 87.23 and Terminal 4.0 36.36, but no complete engineering index."),
    "gemini-4-argon": ("Akita measured high effort on Antigravity. Zero marginal OAuth dollars "
                         "does not mean zero quota consumption or zero API cost."),
}

_CACHE: dict[str, Any] = {"at": 0.0, "index": None}


def openrouter_index(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """``{"models": {id: signals}, "max_coding", "max_intelligence"}`` from coding-ordered catalog rows."""
    models, rank = {}, 0
    for row in rows:
        mid = row.get("id")
        if not isinstance(mid, str) or mid.endswith(":batch"):
            continue
        rank += 1
        aa = (row.get("benchmarks") or {}).get("artificial_analysis") or {}
        pricing = row.get("pricing") or {}
        try:
            # Blended 1:3 input:output, per million tokens.
            blended = round((float(pricing["prompt"]) + 3 * float(pricing["completion"])) / 4 * 1e6, 3)
        except (KeyError, TypeError, ValueError):
            blended = None
        models[mid] = {key: value for key, value in {
            "coding_rank": rank,
            "coding_index": _number(aa.get("coding_index")),
            "intelligence_index": _number(aa.get("intelligence_index")),
            "agentic_index": _number(aa.get("agentic_index")),
            "usd_per_mtok": blended,
        }.items() if value is not None}
    return {"models": models,
            "max_coding": max((m.get("coding_index", 0) for m in models.values()), default=0),
            "max_intelligence": max((m.get("intelligence_index", 0) for m in models.values()), default=0)}


def load(fetch: Callable[[], list[dict[str, Any]]], snapshot: Path, now: float | None = None) -> dict[str, Any]:
    """The OpenRouter index: in-memory for ``TTL_SECONDS``, else fetched (and snapshotted), else the
    last snapshot, else empty. Never raises: the static v4 data still routes without it."""
    now = time.time() if now is None else now
    if _CACHE["index"] is not None and now - _CACHE["at"] < TTL_SECONDS:
        return _CACHE["index"]
    try:
        index = openrouter_index(fetch())
        if not index["models"]:
            raise ValueError("empty catalog")
        index["fetched_at"] = int(now)
        try:
            snapshot.write_text(json.dumps(index, separators=(",", ":")), encoding="utf-8")
        except OSError:
            pass
    except Exception:
        try:
            index = json.loads(snapshot.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            index = {"models": {}, "max_coding": 0, "max_intelligence": 0}
    _CACHE.update(at=now, index=index)
    return index


def route_key(candidate: dict[str, Any]) -> str:
    model = str(candidate.get("model") or "")
    if candidate.get("cli") == "agy":
        return re.sub(r"-(low|medium|high)$", "", model)
    return model


def profile(candidate: dict[str, Any], index: dict[str, Any]) -> dict[str, Any] | None:
    """The combined benchmark profile for one route, or None when nothing is known about it."""
    key = route_key(candidate)
    models = index.get("models") or {}
    or_id = OPENROUTER_IDS.get(key) or key
    signals = models.get(or_id) or models.get(or_id.removesuffix(":free"))
    akita_key = key if key in AKITA_V4 else next(
        (route for route, catalog_id in OPENROUTER_IDS.items() if catalog_id == key), key)
    akita = AKITA_V4.get(akita_key)
    engineering = AKITA_ENGINEERING.get(akita_key)
    components, basis = [], []
    if engineering:
        components.append(engineering["score"])
        basis.append("akita_engineering_breadth_90pct")
    elif akita:
        components.append(akita["score"])
        basis.append("akita_v4")
    elif signals:
        if signals.get("coding_index") is not None and index.get("max_coding"):
            components.append(100 * signals["coding_index"] / index["max_coding"])
            basis.append("aa_coding")
        elif signals.get("intelligence_index") is not None and index.get("max_intelligence"):
            components.append(100 * signals["intelligence_index"] / index["max_intelligence"])
            basis.append("aa_intelligence_only")
    result = {}
    if components:
        result["composite_quality"] = round(sum(components) / len(components), 1)
        # A single source, or intelligence without a coding index, is a weaker estimate.
        result["composite_basis"] = basis
    if akita:
        result["akita_v4"] = {"cost_basis": "api_equivalent_campaign_not_quota", **akita}
        result["akita_source"] = AKITA_SOURCE
    if engineering:
        result["akita_engineering"] = engineering
    if akita_key in AKITA_PARTIAL:
        result["akita_partial"] = AKITA_PARTIAL[akita_key]
    if signals:
        result["openrouter"] = signals
    if key in OPERATOR_NOTES:
        result["note"] = OPERATOR_NOTES[key]
    return result or None


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
