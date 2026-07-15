from datetime import date

import quant_system.research_service as research_service_module
from quant_system.providers import DeterministicDemoProvider
from quant_system.research import SensitivityResult, make_time_series_split
from quant_system.research_service import read_research_artifact, read_research_run, run_research_package


def test_demo_research_package_is_immutable_and_fails_closed(tmp_path):
    snapshot = DeterministicDemoProvider().load()
    result = run_research_package(snapshot, tmp_path, capital=100_000)
    assert result["status"] == "completed"
    assert result["overall"] == "FAIL"
    assert result["candidate_label"] == "工程候选版/模拟观察中"
    assert read_research_run(result["id"], tmp_path)["id"] == result["id"]
    gates = read_research_artifact(result["id"], "gates.json", tmp_path)
    statuses = {x["id"]: x["status"] for x in gates["gates"]}
    assert statuses["DAT-PIT"] == "FAIL"
    assert statuses["DATA-LICENSE"] == "FAIL"
    assert statuses["SIM-003-OBSERVATION"] == "FAIL"
    assert statuses["BT-006-BASELINES"] == "FAIL"
    assert statuses["BT-008-EXCESS"] == "FAIL"
    report = read_research_artifact(result["id"], "research_report.json", tmp_path)
    assert len(report["capacity"]) == 4
    assert len(report["sensitivity"]) == 9
    assert report["final_test"]["isolated_from_parameter_selection"] is True
    assert report["final_test"]["executed_after_parameter_freeze"] is True
    assert report["final_test"]["observations"] > 0
    assert report["parameter_experiment_scope"]["end"] < report["final_test"]["start"]
    assert report["parameter_experiment_scope"]["overlaps_final_test"] is False
    assert report["baseline_contract"]["status"] == "missing"
    assert report["baselines"] == []
    assert report["ablation"]["status"] == "completed"
    assert len(report["ablation"]["items"]) == 4


def test_parameter_search_cannot_read_the_declared_final_test_window(tmp_path, monkeypatch):
    """BT-005: sensitivity work must finish before the isolated test is exposed."""
    snapshot = DeterministicDemoProvider().load()
    observed_dates = []

    def capture_sensitivity(training_snapshot, **_kwargs):
        observed_dates.extend(bar.day for bar in training_snapshot.bars)
        return (
            SensitivityResult(
                {"rebalance_days": 10, "slippage_bps": 8.0},
                0.0, 0.0, 0.0, True,
            ),
        )

    monkeypatch.setattr(research_service_module, "parameter_sensitivity", capture_sensitivity)

    result = run_research_package(snapshot, tmp_path, capital=100_000)
    final_test_start = date.fromisoformat(result["report"]["final_test"]["start"])

    assert observed_dates
    assert max(observed_dates) < final_test_start


def test_all_sensitivity_and_ablation_inputs_exclude_final_test(tmp_path, monkeypatch):
    snapshot = DeterministicDemoProvider().load()
    split = make_time_series_split(sorted({bar.day for bar in snapshot.bars}), embargo_observations=2)
    final_start = split.windows[-1].start
    real_runner = research_service_module.run_backtest
    calls = []

    def audited_runner(input_snapshot, **kwargs):
        calls.append({
            "max_date": max(bar.day.isoformat() for bar in input_snapshot.bars),
            "metadata": input_snapshot.metadata,
            "is_parameter_experiment": input_snapshot.metadata.get("research_scope", {}).get("purpose")
            == "parameter_selection_sensitivity_ablation",
        })
        return real_runner(input_snapshot, **kwargs)

    monkeypatch.setattr(research_service_module, "run_backtest", audited_runner)
    run_research_package(snapshot, tmp_path, capital=100_000)
    experiments = [call for call in calls if call["is_parameter_experiment"]]
    assert len(experiments) == 14  # 9 sensitivity + full/4 leave-one-out ablations
    assert all(call["max_date"] < final_start for call in experiments)
    assert all(set(call["metadata"]) == {"research_scope"} for call in experiments)


def test_explicit_baselines_are_evaluated_without_proxy_generation(tmp_path):
    snapshot = DeterministicDemoProvider().load()
    split = make_time_series_split(sorted({bar.day for bar in snapshot.bars}), embargo_observations=2)
    final_dates = [
        day.isoformat() for day in sorted({bar.day for bar in snapshot.bars})
        if day.isoformat() >= split.windows[-1].start
    ][1:]
    snapshot.metadata["research_baselines"] = {
        "schema_version": "research-baselines/v1",
        "series": {
            name: {
                "returns_by_date": {day: .001 for day in final_dates},
                "source": f"explicit-fixture:{name}",
                "is_official_point_in_time": name != "简单动量",
            }
            for name in ("沪深300", "中证全指", "简单动量")
        },
    }
    result = run_research_package(snapshot, tmp_path, capital=100_000)
    report = result["report"]
    assert report["baseline_contract"]["status"] == "available"
    assert len(report["baselines"]) == 3
    assert all(item["source"].startswith("explicit-fixture:") for item in report["baselines"])
