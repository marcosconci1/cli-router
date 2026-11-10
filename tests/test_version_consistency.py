import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_plugin_pyproject_and_changelog_versions_match():
    plugin = re.search(r"^version:\s*['\"]?([^'\"\s]+)", (ROOT / "plugin.yaml").read_text(), re.M).group(1)
    pyproject = re.search(r'^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)
    changelog = re.search(r"^## (\d+\.\d+\.\d+)", (ROOT / "CHANGELOG.md").read_text(), re.M).group(1)
    assert plugin == pyproject == changelog
