"""Picklable probes for isolated readiness tests. Spawn must import this module."""

from __future__ import annotations

import time


def ready_probe(_workflow, _harness, _timeout):
    return {"status": "ready"}


def sleep_probe(_workflow, _harness, _timeout):
    time.sleep(30)
    return {"status": "ready"}
