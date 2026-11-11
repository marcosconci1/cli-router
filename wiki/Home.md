# cli-router wiki

> A task router for Hermes Agent. Submit a coding task, and the AI CLI with real quota room builds it, commits it on your branch, and passes two independent reviews by default before it is done.

Every AI coding CLI carries its own subscription and its own limit, and the limits reset on different schedules. A task that outlives one window stops until the person notices, switches tools, and explains the task again.

cli-router does the switching first. It reads the real usage window of each configured CLI, applies `quota-reserve` (default 30), and sends the task to a route with room. The worker runs inside a sandbox, commits on your branch, and two distinct models in fresh contexts and separate disposable snapshots review the change before it counts as done by default; the review can be switched off in `cli-router.yaml`.

The subscriptions wear out evenly instead of one at a time. By default every change lands with a second opinion recorded in the task history. The first version refused to route without a decision helper the user configures; the public version can rank deterministically without Jev. Retrying a failed work attempt uses Jev's handoff decision when available; without Jev, the retry falls back to a deterministic continue-on-progress decision recorded as a route warning.

## Map

| If you want to | Read |
|---|---|
| Why the router exists, and what it refuses to do | [Context](Context) |
| How it works, and why it is built this way | [Architecture](Architecture) |
