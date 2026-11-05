import importlib.util
import json
import sys
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import quota_providers
from quota_providers import QuotaResult
from quota_providers import claude, codex, kimi


class QuotaProviderTests(unittest.TestCase):
    def test_vendored_providers_import_and_register_cleanly(self):
        for provider_id, module in (("openai-codex", codex), ("kimi", kimi)):
            with self.subTest(provider=provider_id):
                self.assertEqual(module.QuotaResult, quota_providers.QuotaResult)
                self.assertTrue(callable(quota_providers.get_fetcher(provider_id)))

    def test_optional_antigravity_provider_loads_only_when_present(self):
        if importlib.util.find_spec("quota_providers.antigravity") is None:
            self.assertIsNone(quota_providers.get_fetcher("antigravity"))
        else:
            self.assertTrue(callable(quota_providers.get_fetcher("antigravity")))

    def test_codex_parses_usage_and_additional_rate_limits(self):
        payload = {
            "plan_type": "plus",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 21,
                    "limit_window_seconds": 18_000,
                    "reset_at": 1_900_000_000,
                },
            },
            "additional_rate_limits": [{
                "limit_name": "GPT-5.3-Codex-Spark",
                "rate_limit": {
                    "secondary_window": {
                        "used_percent": 12,
                        "reset_at": 1_900_100_000,
                    },
                },
            }],
        }
        credential = ("synthetic-token", "synthetic-account", False, "", None, False)
        with patch.object(codex, "_usage_url", return_value="https://example.invalid/usage"), \
             patch.object(codex, "urlopen_no_redirect",
                          return_value=BytesIO(json.dumps(payload).encode())) as urlopen:
            result = codex._fetch_account(
                credential, codex._parse_codex_payload, codex.Deadline(15))

        self.assertIsNone(result.unavailable_reason)
        self.assertEqual(result.plan, "Plus")
        self.assertEqual([window.label for window in result.windows],
                         ["Session", "5.3 Codex Spark · Weekly"])
        self.assertEqual([window.used_percent for window in result.windows], [21.0, 12.0])
        self.assertEqual(urlopen.call_args.args[0].full_url, "https://example.invalid/usage")

    def test_kimi_parses_ratio_and_rate_windows(self):
        payload = {
            "limits": [{
                "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                "detail": {
                    "limit": "100",
                    "used": "57",
                    "remaining": "43",
                    "resetTime": "2026-09-23T04:08:05.435444Z",
                },
            }],
            "usages": {
                "limit_5h": {"used_ratio": 0, "reset_time": "2026-09-23T05:00:00Z"},
                "limit_month_total": {
                    "used_ratio": 0.1559,
                    "reset_time": "2026-10-01T00:00:00Z",
                },
            },
        }
        with patch.object(kimi, "urlopen_no_redirect",
                          return_value=BytesIO(json.dumps(payload).encode())) as urlopen:
            result = kimi._fetch_with({}, "https://example.invalid/v1/usages")

        self.assertIsNone(result.unavailable_reason)
        self.assertEqual([window.label for window in result.windows],
                         ["Session (5h)", "Monthly total", "Rate (5h)"])
        self.assertEqual([window.used_percent for window in result.windows], [0.0, 15.59, 57.0])
        self.assertEqual(result.windows[-1].reset_at, "2026-09-23T04:08:05.435444Z")
        self.assertEqual(urlopen.call_args.args[0].full_url, "https://example.invalid/v1/usages")


class _Resp:
    def __init__(self, body):
        self._b = body if isinstance(body, bytes) else json.dumps(body).encode()

    def read(self, n=-1):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class ClaudeProviderTests(unittest.TestCase):
    def _fetch(self, response=None, token="tok-secret"):
        import urllib.error
        with patch.object(claude, "_read_token", return_value=token), \
             patch.object(claude, "urlopen_no_redirect", side_effect=response) as urlopen:
            return claude.fetch_claude_quota(), urlopen

    def test_registered(self):
        self.assertTrue(callable(quota_providers.get_fetcher("claude")))

    def test_two_window_payload(self):
        payload = {"five_hour": {"utilization": 40, "resets_at": "2026-11-03T10:00:00Z"},
                   "seven_day": {"utilization": 75.5, "resets_at": None}}
        result, urlopen = self._fetch(lambda *a, **k: _Resp(payload))
        self.assertIsNone(result.unavailable_reason)
        self.assertEqual([(w.label, w.used_percent) for w in result.windows],
                         [("Current session", 40.0), ("Current week", 75.5)])
        self.assertEqual(result.windows[0].reset_at, "2026-11-03T10:00:00+00:00")
        self.assertEqual(result.windows[1].remaining_pct(), 24)
        req = urlopen.call_args[0][0]
        self.assertEqual(req.get_header("Anthropic-beta"), "oauth-2025-04-20")
        self.assertEqual(req.get_header("Authorization"), "Bearer tok-secret")
        self.assertNotIn("tok-secret", repr(result))
        self.assertEqual(len(result.account_label), 12)

    def test_scoped_weekly_limits(self):
        payload = {"five_hour": {"utilization": 10}, "limits": [
            {"kind": "weekly_scoped", "percent": 33,
             "scope": {"model": {"display_name": "Sonnet"}}},
            {"kind": "weekly_all", "percent": 50},
        ]}
        result, _ = self._fetch(lambda *a, **k: _Resp(payload))
        got = {w.label: w.used_percent for w in result.windows}
        self.assertEqual(got, {"Current session": 10.0, "Sonnet week": 33.0, "Current week": 50.0})

    def test_http_429_is_unavailable(self):
        import urllib.error
        err = urllib.error.HTTPError("u", 429, "Too Many", {}, None)
        result, _ = self._fetch(err)
        self.assertEqual(result.unavailable_reason, "http-429")
        self.assertEqual(result.windows, [])

    def test_missing_credentials(self):
        with patch.object(claude, "_credentials_path", return_value="/nonexistent/x.json"):
            result = claude.fetch_claude_quota()
        self.assertEqual(result.unavailable_reason, "no-credentials")

    def test_malformed_json(self):
        result, _ = self._fetch(lambda *a, **k: _Resp(b"{not json"))
        self.assertEqual(result.unavailable_reason, "bad-json")

    def _sidecar_source(self, tmp):
        from quota import FileCacheQuotaSource
        Path(tmp, "quota_cache.json").write_text(json.dumps({"fetched_at": None, "providers": {}}))
        return FileCacheQuotaSource(tmp)

    def test_cache_keeps_last_entry_on_unavailable(self):
        import tempfile
        import quota
        from quota_providers.base import build_unavailable
        with tempfile.TemporaryDirectory() as tmp:
            src = self._sidecar_source(tmp)
            good = QuotaResult(label="claude", windows=[claude.QuotaWindow("Current week", 20.0)])
            with patch.dict(quota_providers.PROVIDER_FETCHERS, {"claude": lambda: good}, clear=True):
                src._refresh_vendored()
            with patch.dict(quota._ROUTER_OWNED_INTERVAL_S, {"claude": 0.0}), \
                 patch.dict(quota_providers.PROVIDER_FETCHERS,
                            {"claude": lambda: build_unavailable("claude", "http-429")}, clear=True):
                src._refresh_vendored()
            rec = src.read()["providers"]["claude"]
            self.assertIsNone(rec["unavailable_reason"])
            self.assertEqual(rec["windows"][0]["used_percent"], 20.0)

    def test_claude_entry_survives_a_wholesale_rarf_rewrite(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            src = self._sidecar_source(tmp)
            good = QuotaResult(label="claude", windows=[claude.QuotaWindow("Current week", 48.0)])
            with patch.dict(quota_providers.PROVIDER_FETCHERS, {"claude": lambda: good}, clear=True):
                src._refresh_vendored()
            # rarf's refresh_quota_cache() rewrites the shared file with only its own providers.
            Path(tmp, "quota_cache.json").write_text(json.dumps(
                {"fetched_at": "2026-11-03T17:00:00+00:00", "providers": {"anthropic": {"windows": []}}}))
            providers = src.read()["providers"]
            self.assertEqual(providers["claude"]["windows"][0]["used_percent"], 48.0)
            self.assertIn("anthropic", providers)

    def test_claude_fetch_is_throttled_between_refreshes(self):
        import tempfile
        calls = []
        def fetch():
            calls.append(1)
            return QuotaResult(label="claude", windows=[claude.QuotaWindow("Current week", 10.0)])
        with tempfile.TemporaryDirectory() as tmp:
            src = self._sidecar_source(tmp)
            with patch.dict(quota_providers.PROVIDER_FETCHERS, {"claude": fetch}, clear=True):
                src._refresh_vendored()
                src._refresh_vendored()
            self.assertEqual(len(calls), 1)
            self.assertNotIn("claude", json.loads(Path(tmp, "quota_cache.json").read_text())["providers"])


if __name__ == "__main__":
    unittest.main()
