import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import quota


class QuotaSourceTests(unittest.TestCase):
    def _write_cache(self, root, payload):
        path = Path(root) / "quota_cache.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_reads_cache_file(self):
        with tempfile.TemporaryDirectory() as temp:
            payload = {"fetched_at": "2026-11-02T12:00:00+00:00",
                       "providers": {"kimi": {"windows": [{"used_percent": 20}]}}}
            self._write_cache(temp, payload)
            self.assertEqual(quota.FileCacheQuotaSource(temp).read(), payload)

    def test_missing_cache_file_raises_clear_runtime_error(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(RuntimeError, "quota cache file is missing"):
                quota.FileCacheQuotaSource(temp).read()

    def test_invalid_providers_shape_raises(self):
        with tempfile.TemporaryDirectory() as temp:
            self._write_cache(temp, {"fetched_at": "2026-11-02T12:00:00+00:00", "providers": []})
            with self.assertRaisesRegex(RuntimeError, "providers must be an object"):
                quota.FileCacheQuotaSource(temp).read()

    def test_age_is_computed_from_fetched_at(self):
        with tempfile.TemporaryDirectory() as temp:
            fetched_at = datetime.now(timezone.utc) - timedelta(seconds=30)
            self._write_cache(temp, {"fetched_at": fetched_at.isoformat(), "providers": {}})
            age = quota.FileCacheQuotaSource(temp).age_seconds()
            self.assertIsNotNone(age)
            self.assertGreaterEqual(age, 30)
            self.assertLess(age, 60)

    def test_refresh_without_fetchers_does_not_need_rarf_plugin(self):
        with tempfile.TemporaryDirectory() as temp:
            source = quota.FileCacheQuotaSource(temp)
            with patch.dict(quota.quota_providers.PROVIDER_FETCHERS, {}, clear=True), \
                 patch.object(quota, "_optional_rarf_plugin",
                              side_effect=AssertionError("rarf fallback used")):
                self.assertIsNone(source.refresh())
            self.assertEqual(source.read()["providers"], {})

    def test_refresh_merges_vendored_result_into_existing_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            legacy = {"label": "legacy", "windows": [{"used_percent": 40}]}
            self._write_cache(temp, {"fetched_at": "2026-11-02T12:00:00+00:00",
                                     "providers": {"legacy": legacy,
                                                   "kimi": {"windows": [{"used_percent": 90}]}}})
            result = quota.QuotaResult(
                label="kimi",
                windows=[quota.quota_providers.QuotaWindow(
                    label="Session", used_percent=25, reset_at="2026-11-02T17:00:00Z")],
            )
            source = quota.FileCacheQuotaSource(temp)
            with patch.dict(quota.quota_providers.PROVIDER_FETCHERS,
                            {"kimi": lambda: result}, clear=True), \
                 patch.object(quota, "_optional_rarf_plugin",
                              side_effect=AssertionError("rarf fallback used")):
                source.refresh()

            payload = source.read()
            self.assertEqual(payload["providers"]["legacy"], legacy)
            self.assertEqual(payload["providers"]["kimi"]["label"], "kimi")
            self.assertIsNone(payload["providers"]["kimi"]["unavailable_reason"])
            self.assertEqual(payload["providers"]["kimi"]["windows"], [{
                "label": "Session", "used_percent": 25, "reset_at": "2026-11-02T17:00:00Z",
            }])
            self.assertNotEqual(payload["fetched_at"], "2026-11-02T12:00:00+00:00")

    def test_refresh_fetcher_exception_records_failure_and_uses_rarf_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            legacy = {"label": "legacy", "windows": []}
            self._write_cache(temp, {"fetched_at": None, "providers": {"legacy": legacy}})
            fallback = Mock()

            def fail():
                raise RuntimeError("synthetic fetch failure")

            source = quota.FileCacheQuotaSource(temp)
            with patch.dict(quota.quota_providers.PROVIDER_FETCHERS,
                            {"kimi": fail}, clear=True), \
                 patch.object(quota, "_optional_rarf_plugin", return_value=fallback) as plugin:
                self.assertIsNone(source.refresh())

            plugin.assert_called_once_with(source._home)
            fallback.refresh_quota_cache.assert_called_once_with()
            payload = source.read()
            self.assertEqual(payload["providers"]["legacy"], legacy)
            self.assertEqual(payload["providers"]["kimi"]["unavailable_reason"], "fetch-error")
            self.assertEqual(payload["providers"]["kimi"]["windows"], [])

    def test_hermes_constants_is_not_required(self):
        with tempfile.TemporaryDirectory() as temp:
            payload = {"fetched_at": "2026-11-02T12:00:00+00:00", "providers": {}}
            self._write_cache(temp, payload)
            with patch.dict("os.environ", {"HERMES_HOME": temp}), \
                 patch.dict("sys.modules", {"hermes_constants": None}):
                source = quota.default_source()
                self.assertIsInstance(source, quota.FileCacheQuotaSource)
                self.assertEqual(source.read(), payload)

    def test_manual_quota_source_reads_dotted_setting_directly(self):
        ctx = Mock()
        ctx.get_config.return_value = {
            "kimi": {"windows": [{"used_percent": 15.0}]},
        }
        with patch.object(quota.hermes_adapter, "_CONTEXT", ctx):
            payload = quota.ManualQuotaSource().read()
        self.assertEqual(payload["providers"]["kimi"]["windows"][0]["used_percent"], 15.0)
        ctx.get_config.assert_called_once_with("quota.manual", {})

    def test_manual_quota_source_builds_routes_compatible_windows(self):
        manual_cfg = {
            "quota.manual": {
                "kimi": {
                    "label": "kimi",
                    "windows": [{"label": "5h", "reset_at": "2026-11-03T12:00:00Z", "used_percent": 15.0}],
                },
                "antigravity": {
                    "label": "antigravity",
                    "windows": [{"label": "daily", "reset_at": "2026-11-04T00:00:00Z", "used_percent": 30.0}],
                },
            }
        }
        ctx = Mock()
        ctx.get_config.return_value = manual_cfg
        with patch.object(quota.hermes_adapter, "_CONTEXT", ctx):
            source = quota.ManualQuotaSource()
            payload = source.read()
            self.assertIn("providers", payload)
            self.assertIn("kimi", payload["providers"])
            self.assertEqual(payload["providers"]["kimi"]["windows"][0]["used_percent"], 15.0)

            # Test compatibility with task_manager._subscriptions
            import task_manager
            q = {"cache": payload, "age": 0.0, "fresh": True}
            models = {
                "kimi": {"kimi-code/k3.1-pro": []},
                "agy": {"gemini-4-argon-low": ["low"]},
                "codex": {},
                "claude": {},
            }
            routes = task_manager._subscriptions(q, models)
            self.assertTrue(any(r["cli"] == "kimi" for r in routes))
            self.assertTrue(any(r["cli"] == "agy" for r in routes))

    def test_default_source_selection_picks_manual_only_when_cache_absent(self):
        with tempfile.TemporaryDirectory() as temp:
            # 1. Cache present: FileCacheQuotaSource is chosen even if Hermes context exists
            payload = {"fetched_at": "2026-11-02T12:00:00+00:00", "providers": {}}
            self._write_cache(temp, payload)
            ctx = Mock()
            ctx.get_config.return_value = {"quota.manual": {"kimi": {"windows": []}}}
            with patch.dict("os.environ", {"HERMES_HOME": temp}), \
                 patch.object(quota.hermes_adapter, "_CONTEXT", ctx):
                source = quota.default_source(temp)
                self.assertIsInstance(source, quota.FileCacheQuotaSource)

            # 2. Cache absent: ManualQuotaSource is chosen when inside Hermes
            (Path(temp) / "quota_cache.json").unlink()
            with patch.dict("os.environ", {"HERMES_HOME": temp}), \
                 patch.object(quota.hermes_adapter, "_CONTEXT", ctx):
                source = quota.default_source(temp)
                self.assertIsInstance(source, quota.ManualQuotaSource)

    def test_manual_quota_source_standalone_reads_unsupported(self):
        with patch.object(quota.hermes_adapter, "_CONTEXT", None):
            source = quota.ManualQuotaSource()
            with self.assertRaisesRegex(RuntimeError, "standalone reads are not supported"):
                source.read()


if __name__ == "__main__":
    unittest.main()
