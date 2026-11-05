import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import route_benchmark as bench


class UpdatedBenchmarkTests(unittest.TestCase):
    def test_inline_observations_are_complete_and_sourced(self):
        url = "https://akitaonrails.com/2026/10/28/llm-benchmark-v4-gemini-4-argon-gpt-sol-6-1-terra-kimi-k3-1-pro-claude-sonnet-5-5/"
        self.assertEqual(bench.AKITA_SOURCE, url)
        self.assertIn(url, bench.__doc__ or "")
        self.assertIn("benchmarks/README.md", bench.__doc__ or "")
        self.assertTrue(bench.AKITA_V4)
        for key, row in bench.AKITA_V4.items():
            for field in ("score", "run_cost_usd", "wall_minutes"):
                with self.subTest(model=key, field=field):
                    value = row[field]
                    self.assertIsInstance(value, (int, float))
                    self.assertNotIsInstance(value, bool)
                    if field == "run_cost_usd":
                        # OAuth harness runs report a zero marginal campaign cost.
                        self.assertGreaterEqual(value, 0)
                    else:
                        self.assertGreater(value, 0)

    def test_updated_article_values(self):
        expected = {
            "gemini-4-argon": (100.0, 0.0, 114),
            "gpt-6.1-sol": (97.0, 8.12, 65),
            "gpt-6.1-terra": (96.8, 1.69, 21),
            "kimi-code/k3.1-pro": (92.11, 12.67, 110),
            "claude-sonnet-5-5": (88.0, 3.90, 27),
        }
        for model, values in expected.items():
            with self.subTest(model=model):
                row = bench.AKITA_V4[model]
                self.assertEqual((row["score"], row["run_cost_usd"], row["wall_minutes"]), values)

    def test_akita_is_primary_not_averaged_with_openrouter(self):
        index = bench.openrouter_index([{
            "id": "google/gemini-4-argon",
            "benchmarks": {"artificial_analysis": {"coding_index": 1}},
        }])
        profile = bench.profile({"cli": "agy", "model": "gemini-4-argon-high"}, index)
        assert profile is not None
        self.assertEqual(profile["composite_quality"], 96.6)
        self.assertEqual(profile["composite_basis"], ["akita_engineering_breadth_90pct"])
        self.assertEqual(profile["akita_engineering"]["coverage_percent"], 90)
        self.assertEqual(profile["akita_v4"]["cost_basis"], "oauth_marginal_not_quota")

    def test_unlisted_exact_model_uses_openrouter_not_its_predecessor(self):
        index = bench.openrouter_index([{
            "id": "anthropic/claude-haiku-5.5",
            "benchmarks": {"artificial_analysis": {"coding_index": 50}},
        }])
        profile = bench.profile({"cli": "claude", "model": "claude-haiku-5-5"}, index)
        assert profile is not None
        self.assertEqual(profile["composite_basis"], ["aa_coding"])
        self.assertNotIn("akita_v4", profile)
        self.assertNotIn("akita_engineering", profile)

    def test_gemini_harness_results_remain_distinct(self):
        agy = bench.profile({"cli": "agy", "model": "gemini-3.8-flash-low"}, {})
        api = bench.profile({"cli": "opencode", "model": "google/gemini-3.8-flash"}, {})
        assert agy is not None and api is not None
        self.assertEqual(agy["akita_v4"]["score"], 89.5)
        self.assertEqual(api["akita_v4"]["score"], 90.5)
        self.assertEqual(api["akita_v4"]["run_cost_usd"], 15.98)
        self.assertNotIn("akita_engineering", api)
