# cli-router wiki

> A task router for Hermes Agent. Submit a coding task, and the AI CLI with real quota room builds it, commits it on your branch, and passes two independent reviews before it is done.

Every AI coding CLI carries its own subscription and its own limit, and the limits reset on different schedules. A task that outlives one window stops until the person notices, switches tools, and explains the task again.

cli-router does the switching first. It reads the real usage window of each configured CLI, keeps a 20% reserve, and sends the task to the one with room. The worker runs inside a sandbox, commits on your branch, and two distinct models in fresh contexts and separate disposable snapshots review the change before it counts as done.

The subscriptions wear out evenly instead of one at a time. Every change lands with a second opinion recorded in the task history. The first version refused to route without a decision helper the user configures; the public version can rank deterministically without Jev. Retrying a failed work attempt still requires a Jev handoff decision; without it, the task blocks.

## Map

| If you want to | Read |
|---|---|
| Why the router exists, and what it refuses to do | [Context](Context) |
| How it works, and why it is built this way | [Architecture](Architecture) |
