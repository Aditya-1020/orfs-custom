from asicflow.core.configuration import deep_merge, load_configuration, load_yaml


def test_deep_merge_recurses_and_replaces_sequences():
    merged = deep_merge(
        {"paths": {"work": "build", "logs": "logs"}, "items": [1]},
        {"paths": {"work": "out"}, "items": [2, 3]},
    )
    assert merged == {"paths": {"work": "out", "logs": "logs"}, "items": [2, 3]}


def test_load_yaml_expands_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ASICFLOW_TEST_ROOT", "/tmp/asicflow-test")
    config = tmp_path / "flow.yaml"
    config.write_text("paths:\n  root: $" + "{ASICFLOW_TEST_ROOT}")
    assert load_yaml(config)["paths"]["root"] == "/tmp/asicflow-test"


def test_load_configuration_selects_platform_and_design(tmp_path):
    (tmp_path / "platforms").mkdir()
    (tmp_path / "designs").mkdir()
    (tmp_path / "flow.yaml").write_text("defaults:\n  run_tag: baseline\n")
    (tmp_path / "platforms" / "demo.yaml").write_text(
        "timing:\n  default_liberty: lib/demo.lib\n"
    )
    (tmp_path / "designs" / "chip.yaml").write_text("design:\n  name: chip\n")
    global_cfg, platform_cfg, design_cfg = load_configuration(
        tmp_path, platform="demo", design_name="chip"
    )
    assert global_cfg["defaults"]["run_tag"] == "baseline"
    assert platform_cfg["timing"]["default_liberty"] == "lib/demo.lib"
    assert design_cfg["design"]["name"] == "chip"


def test_load_configuration_merges_builtin_and_project_overrides(tmp_path):
    defaults = tmp_path / "defaults"
    project = tmp_path / "project"
    (defaults / "platforms").mkdir(parents=True)
    (project / "platforms").mkdir(parents=True)
    (defaults / "flow.yaml").write_text(
        "defaults:\n  run_tag: base\npaths:\n  results: build\n"
    )
    (project / "flow.yaml").write_text(
        "defaults:\n  flow_variant: trial\npaths:\n  results: out\n"
    )
    (defaults / "platforms" / "demo.yaml").write_text("timing:\n  corner: slow\n")
    (project / "platforms" / "demo.yaml").write_text(
        "timing:\n  default_liberty: lib/demo.lib\n"
    )

    global_cfg, platform_cfg, design_cfg = load_configuration(
        project, platform="demo", defaults_dir=defaults
    )

    assert global_cfg == {
        "defaults": {"run_tag": "base", "flow_variant": "trial"},
        "paths": {"results": "out"},
    }
    assert platform_cfg == {
        "timing": {"corner": "slow", "default_liberty": "lib/demo.lib"}
    }
    assert design_cfg == {}
