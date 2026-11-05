import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HERMES_DIR = Path(os.environ.get("HERMES_AGENT_DIR", Path.home() / ".hermes" / "hermes-agent")).expanduser()
pytestmark = pytest.mark.skipif(
    not (HERMES_DIR / "hermes_cli" / "plugins.py").is_file(),
    reason="Hermes Agent source is not installed",
)


def _python_candidates():
    installed = list((HERMES_DIR.parent / "installs").glob("*/environments/*/venv/bin/python"))
    installed.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    candidates = [
        *installed,
        *sorted((HERMES_DIR.parent / "tools").glob("python-*/bin/python3")),
        HERMES_DIR / ".venv" / "bin" / "python",
        Path(sys.executable),
    ]
    seen = set()
    for candidate in candidates:
        if candidate.is_file() and candidate not in seen:
            seen.add(candidate)
            yield candidate


def _hermes_json(script, *args):
    errors = []
    for python in _python_candidates():
        env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(HERMES_DIR.parent),
            "PYTHONPATH": str(HERMES_DIR),
        }
        result = subprocess.run(
            [str(python), "-c", script, *map(str, args)],
            capture_output=True,
            text=True,
            timeout=90,
            env=env,
        )
        try:
            return json.loads(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            errors.append(result.stderr.strip())
    return {"unavailable": errors[-1] if errors else "no Python interpreter found"}


def test_register_tool_accepts_the_plugins_keywords():
    result = _hermes_json(
        "import inspect, json; "
        "from hermes_cli.plugins import PluginContext; "
        "print(json.dumps(list(inspect.signature(PluginContext.register_tool).parameters)))"
    )
    assert "unavailable" not in result, result["unavailable"]

    captured = {}
    skills = []

    class Context:
        def register_tool(self, **kwargs):
            captured.update(kwargs)
        def register_skill(self, name, path):
            skills.append(name)

    package_name = "cli_router_hermes_contract"
    spec = importlib.util.spec_from_file_location(
        package_name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = package
    spec.loader.exec_module(package)
    package.register(Context())
    keywords = set(captured) - {"name", "toolset", "schema", "handler", "description", "emoji"}
    assert not keywords
    assert {"name", "toolset", "schema", "handler", "description", "emoji"} <= set(result)
    assert captured["name"] == "cli_router"
    assert captured["toolset"] == "cli_router"
    assert captured["emoji"] == "🔀"


def test_real_hermes_validator_accepts_the_plugin():
    result = _hermes_json(
        "import json; "
        "from pathlib import Path; "
        "from hermes_cli.plugin_validate import validate_plugin_dir; "
        "print(json.dumps(validate_plugin_dir(Path(__import__('sys').argv[1])).to_dict()))",
        ROOT,
    )
    if "unavailable" in result:
        pytest.skip(f"Hermes validator is not importable: {result['unavailable']}")
    assert result["ok"], result["failures"] if "failures" in result else result["checks"]
