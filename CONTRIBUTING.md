# Contributing

Thanks for your interest in cli-router. This page explains how to set up the project and what a good contribution looks like.

## Setup

```bash
git clone https://github.com/marcosconci1/cli-router
cd cli-router
uv sync --locked
uv run --locked python -m pytest tests -q
```

The Hermes contract tests use `~/.hermes/hermes-agent` by default. Set `HERMES_AGENT_DIR` to another source checkout, or they skip when no checkout is present.

See [tests/README.md](tests/README.md) for test organization, fixtures, and focused regression commands.

The plugin is pure standard library Python. New dependencies need a strong reason.

## How to work

1. Open an issue first for anything that changes behavior.
2. Branch from `main`, named by the work: `feat/short-name`, `fix/short-name`, or `docs/short-name`.
3. Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/).
4. Run the test suite before you open a pull request. A change that breaks tests will not merge.
5. Keep pull requests small. One behavior per pull request.

## Documentation

* User facing guides live in `docs/` and stay coupled to the code they describe.
* Design rationale lives in `wiki/`.
* The README stays short. It points into `docs/` and `wiki/` instead of growing new sections.

## Reporting bugs

Use the bug report issue template. If you can, run `python3 <plugin-dir>/doctor.py` and attach its output; it answers most version and environment questions in one shot.

## License

By contributing you agree that your contributions are licensed under the MIT license of this repository.
