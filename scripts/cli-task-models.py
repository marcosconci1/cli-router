#!/usr/bin/env python3
"""Show CLI model usage, task outcomes, and why tasks blocked.

Read-only. Reads the task records under the profile's runtime/cli-tasks directory.
Unreviewed and failed-verification totals remain separate. Owner tokens and hashes are never printed.

    cli-task-models.py                 # last 10 tasks + model totals + block reasons
    cli-task-models.py --last 25
    cli-task-models.py --task <id>     # one task in full (id or prefix)
    cli-task-models.py --json          # machine-readable
"""
import argparse
import collections
import json
import os
import sys
from pathlib import Path


def task_dir():
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return home / "runtime" / "cli-tasks"


def load_tasks(directory):
    tasks = []
    for path in directory.glob("*.json"):
        if path.name == "route-benchmark.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("task_id"):
            tasks.append(data)
    return sorted(tasks, key=lambda t: t.get("created_at") or "")


def route_label(route):
    if not isinstance(route, dict):
        return "?"
    label = f"{route.get('cli', '?')}/{route.get('model', '?')}"
    return label + (f"@{route['effort']}" if route.get("effort") else "")


def reviews(task):
    external = (task.get("verification") or {}).get("external")
    if not isinstance(external, dict):
        return []
    out = []
    for review in external.get("reviews") or []:
        out.append({"route": route_label(review.get("route")), "policy": review.get("policy"),
                    "passed": review.get("passed"), "outcome": review.get("worker_outcome")})
    return out


def summarize(task):
    return {
        "task_id": task["task_id"],
        "created_at": task.get("created_at"),
        "state": task.get("state"),
        "mode": task.get("mode"),
        "repository": Path(task.get("repository") or "?").name,
        "message": task.get("message"),
        "attempts": [{"attempt": a.get("attempt"), "route": route_label(a), "outcome": a.get("outcome")}
                     for a in task.get("attempts") or []],
        "reviews": reviews(task),
        "ai_memory_workstream": (task.get("ai_memory") or {}).get("workstream_name"),
        "handoff": {k: (task.get("ai_memory") or {}).get(k) for k in ("handoff_decision", "handoff_to")
                    if (task.get("ai_memory") or {}).get(k) is not None},
    }


def totals(tasks):
    models = collections.defaultdict(collections.Counter)
    blocked = collections.Counter()
    states = collections.Counter()
    for task in tasks:
        states[task.get("state")] += 1
        if task.get("state") == "blocked":
            blocked[(task.get("message") or "?")[:100]] += 1
        for attempt in task.get("attempts") or []:
            models[route_label(attempt)][attempt.get("outcome") or "?"] += 1
        for review in reviews(task):
            models[review["route"] + " (review)"][review["outcome"] or "?"] += 1
    return states, models, blocked


def render(tasks, last, one):
    lines = []
    if one:
        for task in tasks:
            if task["task_id"].startswith(one):
                lines.append(json.dumps(summarize(task), indent=2, ensure_ascii=False))
        return "\n".join(lines) or f"no task starts with {one}"
    states, models, blocked = totals(tasks)
    lines.append(f"{len(tasks)} tasks: " + ", ".join(f"{n} {s}" for s, n in states.most_common()))
    lines.append("")
    lines.append("Models used (attempts by outcome):")
    for label, counter in sorted(models.items(), key=lambda kv: -sum(kv[1].values())):
        lines.append(f"- {label}: " + ", ".join(f"{n} {o}" for o, n in counter.most_common()))
    if blocked:
        lines.append("")
        lines.append("Why tasks blocked:")
        for message, n in blocked.most_common():
            lines.append(f"- {n}x {message}")
    lines.append("")
    lines.append(f"Last {min(last, len(tasks))} tasks:")
    for task in tasks[-last:]:
        s = summarize(task)
        chain = " -> ".join(f"{a['route']} [{a['outcome']}]" for a in s["attempts"]) or "no attempt started"
        review = (" | review: " + ", ".join(f"{r['route']} [{'pass' if r['passed'] else r['outcome'] or 'fail'}]"
                                           for r in s["reviews"])) if s["reviews"] else ""
        when = (s["created_at"] or "")[:16].replace("T", " ")
        lines.append(f"- {when} {s['task_id'][:8]} {s['state']} ({s['mode']}, {s['repository']}): {chain}{review}")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--last", type=int, default=10)
    parser.add_argument("--task")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--dir", type=Path, default=None)
    args = parser.parse_args(argv)
    tasks = load_tasks(args.dir or task_dir())
    if args.json:
        states, models, blocked = totals(tasks)
        print(json.dumps({"states": states, "models": {k: dict(v) for k, v in models.items()},
                          "blocked_reasons": blocked, "tasks": [summarize(t) for t in tasks[-args.last:]]},
                         indent=2, ensure_ascii=False))
        return 0
    print(render(tasks, args.last, args.task))
    return 0


if __name__ == "__main__":
    sys.exit(main())
