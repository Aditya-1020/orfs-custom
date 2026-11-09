from types import SimpleNamespace

import asicflow.sweep.rank as rank
from asicflow.sweep.rank import normalize_metric


def test_normalize_metric_maximize_and_minimize():
    values = [0.5, 0.1, 0.3]
    assert normalize_metric(values) == [1.0, 0.0, 0.5]
    assert normalize_metric(values, minimize=True) == [0.0, 1.0, 0.5]


def test_normalize_metric_missing_and_constant_values():
    assert normalize_metric([None, 2.0, 4.0]) == [0.0, 0.0, 1.0]
    assert normalize_metric([3.0, 3.0], minimize=True) == [0.0, 0.0]
    assert normalize_metric([None, None]) == [0.0, 0.0]


def test_find_best_configuration_rewards_lower_costs(monkeypatch, tmp_path):
    from asicflow.schema import AREA_STD, FMAX_MHZ, POWER_TOTAL, SETUP_WNS, UTIL_ALL

    better = {
        SETUP_WNS.key: 0.0,
        FMAX_MHZ.key: 200.0,
        AREA_STD.key: 100.0,
        POWER_TOTAL.key: 0.010,
        UTIL_ALL.key: 35.0,
    }
    worse = {
        SETUP_WNS.key: 0.0,
        FMAX_MHZ.key: 200.0,
        AREA_STD.key: 200.0,
        POWER_TOTAL.key: 0.020,
        UTIL_ALL.key: 65.0,
    }
    snapshots = {
        "smaller": SimpleNamespace(
            run_tag="smaller",
            num=lambda key: better.get(getattr(key, "key", key)),
        ),
        "larger": SimpleNamespace(
            run_tag="larger",
            num=lambda key: worse.get(getattr(key, "key", key)),
        ),
    }
    monkeypatch.setattr(rank, "load_snapshot", lambda **kwargs: snapshots[kwargs["run_tag"]])

    report = rank.find_best_configuration(
        db=tmp_path / "metrics.db",
        platform="demo",
        run_tags=["larger", "smaller"],
    )

    assert report.sections["ranking"][0]["run_tag"] == "smaller"
