# Changelog

## 0.4.1

- Add a tokenless `recover` action: reaps dead tasks of a repository to `interrupted` with a checkpoint capture, guarded by the repository lock, so a crashed orchestrator no longer needs the owner token to clean up.
- Make the Jev handoff gate resilient: the assessment retries once and falls back to a deterministic continue-on-progress decision recorded as a route warning, instead of blocking tasks whose work is already done.
- Let analysis-mode tasks share the repository lock under a shared non-blocking lock while change tasks keep exclusivity; conflicting submissions get an immediate busy error and analysis submits never reap orphaned tasks.
- Check the repair-attempt marker against every fetched ledger event before rendering the bounded context, so byte-limit truncation can no longer hide the marker and block the repair leg.

## 0.4.0

- Initial public release. Routes coding tasks across Codex, Claude Code, Kimi and Antigravity with quota-aware rotation, sandboxed execution and independent review.
