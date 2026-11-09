import sys

from asicflow.core.runner import run_command


def test_run_command_success():
    result = run_command([sys.executable, "-c", "print('ok')"])
    assert result.returncode == 0


def test_run_command_nonzero_without_check():
    result = run_command([sys.executable, "-c", "raise SystemExit(3)"], check=False)
    assert result.returncode == 3
