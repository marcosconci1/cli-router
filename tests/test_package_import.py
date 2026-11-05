import importlib
import importlib.util
import sys
import types
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]


class PackageImportTest(unittest.TestCase):
    def test_bridge_loads_when_plugin_is_imported_as_a_package(self):
        # Hermes imports plugins as packages; a bare ``import ai_memory_bridge`` then fails and
        # every cli_router call blocks with "ai-memory prerequisite failed".
        name = "hermes_plugins_probe.cli_router"
        parent = types.ModuleType("hermes_plugins_probe")
        parent.__path__ = []
        spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / "__init__.py",
                                                      submodule_search_locations=[str(PLUGIN_DIR)])
        module = importlib.util.module_from_spec(spec)
        saved_path = list(sys.path)
        sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != PLUGIN_DIR]
        sys.modules.update({"hermes_plugins_probe": parent, name: module})
        try:
            spec.loader.exec_module(module)
            task_manager = importlib.import_module(name + ".task_manager")
            self.assertIsNotNone(task_manager.ai_memory_bridge)
            self.assertEqual(task_manager.ai_memory_bridge.__name__, name + ".ai_memory_bridge")
        finally:
            sys.path[:] = saved_path
            for key in [k for k in sys.modules if k.startswith("hermes_plugins_probe")]:
                del sys.modules[key]


if __name__ == "__main__":
    unittest.main()
