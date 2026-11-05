---
name: Bug report
about: Report something that does not work as documented
labels: bug
---

**What happened**

A clear description of the problem.

**What you expected**

What should have happened instead.

**Steps to reproduce**

1. Submit a task with ...
2. Poll with ...
3. The result is ...

**Environment**

* OS and kernel (`uname -a`):
* cli-router version (from `plugin.yaml`):
* Provider CLIs installed and authenticated:
* ai-memory version (`ai-memory --version`):
* Hermes Agent version:

**Doctor output**

Run `python3 <plugin-dir>/doctor.py` and paste the output here.

```text
(paste output)
```

**Task record**

If a task was involved, paste the relevant part of `runtime/cli-tasks/<task_id>.json` (attempt history and verdicts). Remove anything that looks like a credential first.
