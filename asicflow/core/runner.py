"""One subprocess boundary for EDA tools and helper programs."""
from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping, Sequence

from .errors import FlowError


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int


def run_command(
    command: Sequence[str | Path],
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | str | None = None,
    check: bool = True,
    timeout: float | None = None,
    log_file: Path | str | None = None,
) -> CommandResult:
    """Execute without a shell and optionally redirect combined output to a log."""
    argv = tuple(str(part) for part in command)
    if not argv:
        raise ValueError("command must not be empty")
    print("$ " + shlex.join(argv), flush=True)
    try:
        if log_file is None:
            result = subprocess.run(
                argv, cwd=cwd, env=dict(env) if env is not None else None,
                timeout=timeout, check=False,
            )
        else:
            path = Path(log_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w") as stream:
                result = subprocess.run(
                    argv, cwd=cwd, env=dict(env) if env is not None else None,
                    stdout=stream, stderr=subprocess.STDOUT, timeout=timeout, check=False,
                )
    except FileNotFoundError as exc:
        raise FlowError(f"executable not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise FlowError(f"command timed out after {timeout}s: {shlex.join(argv)}") from exc
    outcome = CommandResult(argv=argv, returncode=result.returncode)
    if check and outcome.returncode:
        suffix = f"; log: {log_file}" if log_file else ""
        raise FlowError(
            f"command exited with status {outcome.returncode}: {shlex.join(argv)}{suffix}"
        )
    return outcome
