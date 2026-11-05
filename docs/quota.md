# Quota behavior

The router picks routes from providers that actually have quota room. This page explains where quota data comes from and what happens when a fetch fails.

## Where quota data comes from

* Vendored fetchers read the real usage windows of Codex, Kimi, and Claude. No third-party proxy sits in the middle. Antigravity (`agy`) remains a route, but its quota is not read in this release: it reads as quota unknown and falls back to the deterministic ranking.
* Every source writes into one router-owned cache, `quota_cache.json`. Writes are atomic, so concurrent readers never see a half written file.
* The Claude record lives in a router-owned sidecar, `cli_router_quota.json`. It is fetched at most every 5 minutes, because the Anthropic usage endpoint returns 429 under polling.

## How the router treats the numbers

* Usage windows are rolling percentages, not absolute counts. Anthropic's endpoint reports only percentages.
* The router keeps a 20% reserve on every provider when usage is known, and never picks a route that is about to hit its limit.
* A fetch that fails keeps the last good entry. Aggressive 429 handling is fail open: the route stays eligible instead of disappearing.
* Until a first Claude fetch succeeds, Claude routes stay eligible with an "unknown quota" reason.

## Manual fallback

When `quota_cache.json` is missing or empty, for example on a fresh install before the first fetch cycle, the router falls back to manual limits under `quota.manual` in the plugin settings. This keeps routing working from minute one.

## Why is there no AGY quota fetcher?

The Antigravity (`agy`) quota fetcher is intentionally left out of the public distribution: keeping vendor-specific endpoint access out of published code avoids credential-scanner false positives on hosts like GitHub. AGY stays a fully supported route; its quota reads as unknown and the deterministic ranking covers it.

If you need live AGY quota numbers, add your own fetcher module as `quota_providers/antigravity.py`: the package imports optional provider modules when present and skips them cleanly when absent. Fetchers follow the fail-open contract in `quota_providers/registry.py`: every failure returns a structured unavailable result, never an exception.

## Ranking

* Default: candidates rank by a public coding benchmark index shipped in `benchmarks/`. Cost breaks ties.
* Optional: the Jev decision plugin ranks with a live LLM call. When Jev is absent or unconfigured, the router uses the deterministic ranking and `doctor.py` reports a ranking warning. Work retries require Jev; without a successful handoff decision, a failed attempt blocks instead of starting another worker.
* A Jev ranking failure adds a `route_warning` event and a `route_warnings` entry to the task record, identifying work or review ranking and the exception class. Exception messages and task prompts are omitted. Successful choices appear as `jev_answer` events, so a task can show both successful Jev decisions and fallback for later choices.
* The `routing.providers` setting turns single CLIs on or off. An absent entry means enabled.
