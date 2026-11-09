from asicflow.core.paths import PathLayout, resolve_path


def test_resolve_path_formats_and_resolves_relative_paths(tmp_path):
    result = resolve_path(
        "build/{platform}/{design}/{run_tag}",
        base=tmp_path,
        values={"platform": "sky130hd", "design": "chip", "run_tag": "base"},
    )
    assert result == tmp_path / "build/sky130hd/chip/base"


def test_path_layout_uses_configured_metrics_database(tmp_path):
    layout = PathLayout(
        project_root=tmp_path,
        work_home=tmp_path / "work",
        platform="demo",
        design="chip",
        flow_variant="base",
        fakeram_tech="demo",
        run_tag="base",
        settings={"metrics_db": "state/{platform}/{design}/{run_tag}.db"},
        package_tcl=tmp_path / "asicflow/tcl",
    )
    assert layout.metrics_db == tmp_path / "state/demo/chip/base.db"
    assert layout.results_dir == tmp_path / "work/results/demo/chip/base"
