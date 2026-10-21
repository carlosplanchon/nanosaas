#!/usr/bin/env python3
"""
The healthcheck of the worker, which compose.yaml runs in its container:
it fails unless each of the worker's processes wrote its heartbeat, every 5
seconds while it can use PostgreSQL and Redis (see beat() in taskiq_task.py),
in the last 30 seconds. They stop when PostgreSQL or Redis doesn't answer,
and when a task with blocking code stops a process's event loop.

    python scripts/worker_healthcheck.py [seconds]

Standalone, so the check doesn't load the app every time it runs.
"""
import sys
import tempfile
import time
from pathlib import Path

limit = float(sys.argv[1]) if len(sys.argv) > 1 else 30
beats = {path.name: time.time() - path.stat().st_mtime for path in Path(tempfile.gettempdir()).glob("nanosaas-heartbeat-*")}
late = {name: age for name, age in beats.items() if age > limit}
if not beats:
    sys.exit("No process of the worker has written its heartbeat")
if late:
    sys.exit("No heartbeat for " + ", ".join(f"{name} in {age:.0f} seconds" for name, age in sorted(late.items())))
