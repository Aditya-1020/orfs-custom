from asicflow.core.stages import (
    ORFS_STAGE_ORDER,
    canon_stage,
    find_sdc,
    stage_major,
    stage_stem,
)


def test_stage_aliases_and_stems():
    assert canon_stage("final") == "finish"
    assert stage_stem("final") == "6_final"
    assert stage_stem("3_place.odb") == "3_place"
    assert stage_major("3_3_place_gp") == 3
    assert ORFS_STAGE_ORDER[-1] == "finish"


def test_find_sdc_selects_latest_not_after_stage(tmp_path):
    earlier = tmp_path / "2_floorplan.sdc"
    later = tmp_path / "5_route.sdc"
    earlier.touch()
    later.touch()
    assert find_sdc(tmp_path, "3_place") == earlier
    assert find_sdc(tmp_path, "1_synth") is None
