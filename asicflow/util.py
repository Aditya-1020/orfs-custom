"""Shared helpers: logging, subprocess, timestamps, run ids, FlowError."""
from __future__ import annotations

import subprocess
import uuid
from datetime import datetime, timezone


class FlowError(RuntimeError):
    """Expected, user-facing failure (bad input, stale ODB, missing file)."""


def log(msg: str) -> None:
    print(msg, flush=True)


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]


def run(cmd: list[str], *, env: dict | None = None, check: bool = True) -> int:
    log("$ " + " ".join(cmd))
    code = subprocess.run(cmd, env=env).returncode
    if check and code != 0:
        raise FlowError(f"exit {code}: {' '.join(cmd)}")
    return code
