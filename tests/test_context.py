from asicflow.config import make_context


def test_make_context_merges_project_configuration_and_exports_paths(tmp_path, monkeypatch):
    project = tmp_path / "design"
    config = project / "config"
    (config / "platforms").mkdir(parents=True)
    (config / "designs").mkdir()
    (project / "design-configs").mkdir()
    orfs = tmp_path / "orfs" / "flow"
    (orfs / "platforms" / "demo").mkdir(parents=True)

    (config / "flow.yaml").write_text(
        "defaults:\n  platform: demo\n  run_tag: trial\n  flow_variant: tuned\n"
        "paths:\n  work_home: output/{platform}/{design}/{run_tag}\n"
    )
    (config / "platforms" / "demo.yaml").write_text(
        "timing:\n  default_liberty: lib/demo.lib\n"
    )
    (config / "designs" / "chip.yaml").write_text(
        "design:\n  name: chip\nmacros: []\n"
    )
    design_config = project / "design-configs" / "demo.mk"
    design_config.write_text(
        "export PLATFORM = demo\n"
        "export DESIGN_NICKNAME = chip\n"
        "export FAKERAM_TECH = demo\n"
    )

    monkeypatch.setenv("ROOT_DIR", str(project))
    monkeypatch.setenv("ORFS_FLOW", str(orfs))
    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("RUN_TAG", raising=False)
    monkeypatch.delenv("FLOW_VARIANT", raising=False)

    ctx = make_context()

    assert ctx.platform == "demo"
    assert ctx.design_nickname == "chip"
    assert ctx.run_tag == "trial"
    assert ctx.flow_variant == "tuned"
    assert ctx.work_home == project / "output/demo/chip/trial"
    assert ctx.platform_dir == orfs / "platforms/demo"
    assert ctx.default_liberty() == ctx.platform_dir / "lib/demo.lib"
    assert ctx.orfs_env()["ORFS_FLOW"] == str(orfs)
    assert ctx.orfs_env()["PDK_ROOT"] == str(project / "pdk")
