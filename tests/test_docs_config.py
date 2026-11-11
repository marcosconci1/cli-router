import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _example_keys():
    top, providers = [], []
    in_providers = False
    for line in (ROOT / "cli-router.example.yaml").read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        key = line.strip().split(":", 1)[0]
        if indent == 2:
            top.append(key)
            in_providers = key == "providers"
        elif indent == 6 and in_providers:
            providers.append(key)
    return top, providers


def test_example_keys_documented():
    text = (ROOT / "docs" / "configuration.md").read_text()
    top, providers = _example_keys()
    assert "providers" in top and "enabled" in providers
    for key in top + providers:
        assert key in text, key


def test_example_does_not_choose_jev_or_ai_memory_by_default():
    lines = (ROOT / "cli-router.example.yaml").read_text().splitlines()
    assert not any(re.match(r"\s+jev:\s*true\s*$", line) for line in lines)
    assert not any(re.match(r"\s+ai-memory:\s*auto\s*$", line) for line in lines)


def test_relative_markdown_links_exist():
    files = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
    for path in files:
        for target in re.findall(r"\]\(([^)\s]+)\)", path.read_text()):
            if re.match(r"^[a-z]+:|^#", target):
                continue
            rel = target.split("#", 1)[0]
            assert (path.parent / rel).exists(), f"{path.name}: {target}"
