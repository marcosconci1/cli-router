# Tests

Run the suite from the repository root:

```bash
uv run --locked python -m pytest tests -q
```

Tests are grouped by behavior: provider quota, routing, worker policy, review
resilience, orchestration, and CLI contracts. Add regressions to the relevant
module; give a new protocol boundary its own module instead of growing
`test_policy.py` indefinitely.

`fixtures/` contains reusable, synthetic protocol examples without credentials,
private prompts, or captured user sessions. `fixtures/codex/` models native JSONL
events for a successful review and a stale answer with no tool execution.
Both parser tests and worker/review regressions use these examples.

Run the Codex and Jev regressions with:

```bash
uv run --locked python -m pytest tests/test_codex_output.py tests/test_policy.py tests/test_routing_learning.py tests/test_jev_fallback.py tests/test_orchestration.py -q
```

Parser tests need no provider CLI or authentication. Worker and orchestration
tests replace process launches and service calls, using temporary repositories
and task storage. They verify routing and review behavior without model requests.
Hermes contract tests need a local Hermes source checkout and skip if absent.
Quota viewer compatibility tests skip when the optional sibling plugin is absent.

Following the separation in [ai-memory's tests](https://github.com/akitaonrails/ai-memory/tree/main/tests),
future tests that launch the installed stack belong in `e2e/`, with isolated
configuration and explicit prerequisites. Keep those distinct from mocked
orchestration regressions. A passing offline suite does not establish that live
provider authentication, quota fetching, or Jev connectivity works.
