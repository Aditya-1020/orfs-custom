"""Compatibility entry point for ASICFlow metrics.

Host: python -m asicflow.metrics place --runs baseline
Worker: openroad -python asicflow/metrics.py <job.json>
"""
import os
import sys
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from asicflow.metric_engine.collector import *
from asicflow.metric_engine.worker import worker_main, _run_worker
from asicflow.metric_engine.discovery import *
from asicflow.metric_engine.reporting import print_summary
from asicflow.metric_engine.store import *
from asicflow.metric_engine.service import driver_main

ROOT = Path(__file__).resolve().parent.parent
WORKER = Path(__file__).resolve()

if __name__ == "__main__":
    if os.environ.get("ORFS_METRICS_JOB") or (
        len(sys.argv) >= 2 and Path(sys.argv[1]).suffix.lower() in {".json", ".odb"}
    ):
        _run_worker()
    else:
        sys.exit(driver_main())
