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
    config.write_text("paths:\n  root: ${ASICFLOW_TEST_ROOT}\n")
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
