from asicflow.plugins.tcl import TclExtensionError, resolve_tcl_extensions, write_tcl_loader


def test_tcl_extensions_are_explicit_and_deduplicated(tmp_path):
    extension = tmp_path / "check.tcl"
    extension.write_text("puts ok\n")
    result = resolve_tcl_extensions(
        project_root=tmp_path,
        flow_settings={"extensions": {"tcl": ["check.tcl"]}},
        design_settings={"extensions": {"tcl": ["check.tcl"]}},
        environ={},
    )
    assert result == [extension]


def test_missing_tcl_extension_fails(tmp_path):
    try:
        resolve_tcl_extensions(
            project_root=tmp_path,
            flow_settings={"extensions": {"tcl": ["missing.tcl"]}},
            environ={},
        )
    except TclExtensionError:
        pass
    else:
        raise AssertionError("missing configured extension should fail")


def test_tcl_loader_sources_only_configured_files(tmp_path):
    extension = tmp_path / "check macro.tcl"
    extension.write_text("puts ok\n")
    loader = write_tcl_loader(tmp_path / "out/load.tcl", [extension])
    assert loader is not None
    text = loader.read_text()
    assert "source $extension" in text
    assert str(extension) in text
