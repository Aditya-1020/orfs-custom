"""Shared logging, timestamp, run-id and process-execution helpers."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .core.errors import FlowError
from .core.runner import run_command


def log(message: str) -> None:
    print(message, flush=True)


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


def run(cmd: list[str], *, env: dict | None = None, check: bool = True) -> int:
    return run_command(cmd, env=env, check=check).returncode
