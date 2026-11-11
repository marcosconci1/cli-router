import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import task_manager as tm

INDEX = {"models": {}, "max_coding": 0, "max_intelligence": 0}
C = [{"cli": "x", "model": "unknown-a"}, {"cli": "claude", "model": "claude-sonnet-5-5"},
     {"cli": "claude", "model": "claude-opus-5-5"}, {"cli": "x", "model": "unknown-b"}]


def run(**kw):
    with patch.object(tm, "_benchmark_index", return_value=INDEX):
        return tm._jev_choose("t", C, **kw)


class Ctx:
    def __init__(self, conn): self.conn = conn
    def get_config(self, name, default=None): return self.conn


class JevFallback(unittest.TestCase):
    def test_direct_plugin_false_skips_service(self):
        class Context:
            def get_config(self, key, default=None):
                return {"jev": False, "connection": {"backend": "openrouter", "model": "typesafe/jev-1.14"}}.get(key, default)
        with patch.object(tm.hermes_adapter, "_CONTEXT", Context()), \
             patch.object(tm, "_load_modular_config", return_value={}), \
             patch.object(tm.hermes_adapter, "_jev_service_cls",
                          side_effect=AssertionError("disabled Jev service loaded")), \
             patch.object(tm, "_benchmark_index", return_value=INDEX):
            self.assertFalse(tm._jev_enabled())
            self.assertTrue(tm._jev_choose("task", C))

    def test_plugin_missing(self):
        with patch.object(tm.hermes_adapter, "_CONTEXT", None):
            out = run()
        self.assertEqual([c["model"] for c in out],
                         ["claude-opus-5-5", "claude-sonnet-5-5", "unknown-a", "unknown-b"])

    def test_unconfigured(self):
        class S:
            def __init__(self, ctx): pass
            def run(self, r): raise AssertionError("must not call")
        with patch.object(tm.hermes_adapter, "_CONTEXT", Ctx({})), \
             patch.object(tm.hermes_adapter, "_jev_service_cls", return_value=S):
            out = run()
        self.assertTrue(out)
        self.assertEqual(out[0]["model"], "claude-opus-5-5")

    def test_configured_jev_wins(self):
        class S:
            def __init__(self, ctx): pass
            def run(self, r):
                return {"ok": True, "answers": {"route": {"choice": "route_0"}}}
        ctx = Ctx({"backend": "openrouter", "model": "typesafe/jev-1.14"})
        cands = C[:2]
        with patch.object(tm.hermes_adapter, "_CONTEXT", ctx), \
             patch.object(tm.hermes_adapter, "_jev_service_cls", return_value=S), \
             patch.object(tm, "_benchmark_index", return_value=INDEX):
            out = tm._jev_choose("t", cands)
        self.assertEqual(out, cands)

    def test_missing_from_index_rank_last(self):
        with patch.object(tm.hermes_adapter, "_CONTEXT", None):
            out = run()
        self.assertEqual([c["model"] for c in out[-2:]], ["unknown-a", "unknown-b"])

    def test_design_arena_best_matching_elo_primary_for_visual_tasks(self):
        index = {"models": {
            "vendor/low": {"design": {"website": 1100, "uicomponent": 1200}},
            "vendor/high": {"design": {"website": 1300}},
        }, "max_coding": 0, "max_intelligence": 0}
        candidates = [{"cli": "opencode", "model": "vendor/low"},
                      {"cli": "opencode", "model": "vendor/high"}]
        ranked = tm._fallback_rank(candidates, index, "Build a website UI")
        self.assertEqual(ranked, [candidates[1], candidates[0]])

    def test_fallback_records_route_warning_without_task_prompt(self):
        task_id = "a" * 32
        marker = "private-routing-prompt"
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(tm, "_root", return_value=Path(temp)), \
             patch.object(tm.hermes_adapter, "_CONTEXT", None), \
             patch.object(tm, "_benchmark_index", return_value=INDEX):
            data = {"task_id": task_id, "state": "running", "events": []}
            tm._write(task_id, data)
            out = tm._jev_choose(marker, C[:1], purpose="review", task_id=task_id,
                                 in_memory_data=data)
            tm._transition(data, "queued", "routing_finished",
                           route_warnings=data["route_warnings"])
            saved = tm._read(task_id)
        self.assertEqual(out, C[:1])
        self.assertEqual(saved["route_warnings"], [
            "Jev review ranking failed (RuntimeError); deterministic benchmark fallback used",
        ])
        warning = next(event for event in saved["events"] if event["event"] == "route_warning")
        self.assertEqual(warning["message"], saved["route_warnings"][0])
        self.assertNotIn(marker, str(saved))


if __name__ == "__main__":
    unittest.main()
