import pytest

from asicflow.core.paths import resolve_path


def test_resolve_path_formats_tokens(tmp_path):
    result = resolve_path(
        "build/{platform}/{run_tag}",
        base=tmp_path,
        values={"platform": "sky130hd", "run_tag": "trial"},
    )
    assert result == tmp_path / "build/sky130hd/trial"


def test_unresolved_environment_variable_is_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv("ASICFLOW_MISSING", raising=False)
    with pytest.raises(ValueError, match="unresolved environment variable"):
        resolve_path("${ASICFLOW_MISSING}/orfs", base=tmp_path)
