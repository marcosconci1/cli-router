# Context

## The situation

Developers who use AI coding CLIs hold several subscriptions at once. Each CLI has its own usage window, and the windows reset on different schedules. The tools are strong, but the daily work of tracking them lands on the person.

## The complication

A task rarely fits inside one window. The CLI in use hits its limit in the middle of the work. The person notices, switches tools by hand, restarts the task, and explains the context again. While one subscription is exhausted, the others sit idle. Manual switching also breaks the review habit: when switching is manual, the second opinion is the first thing people skip.

## The resolution

cli-router makes the switch automatic and, by default, the review mandatory. It reads real usage windows, picks the CLI with room, runs the work in a sandbox, and holds every change until two independent reviews pass by default. The review can be switched off in `cli-router.yaml`. The subscriptions wear out evenly, and by default every change carries a second opinion.

## What this system refuses to do

* No worker outside the sandbox
* By default, no change counted as done without two passing reviews (a skipped review is recorded as skipped)
* No task data sent beyond the configured provider CLIs, Jev, and ai-memory
