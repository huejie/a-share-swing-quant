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
    assert report["runtime"]["python"]
    assert len(report["runtime"]["uv_lock_sha256"]) == 64
    assert len(report["capacity"]) == 4
    assert len(report["sensitivity"]) == 9
    assert report["final_test"]["isolated_from_parameter_selection"] is True
    assert report["final_test"]["executed_after_parameter_freeze"] is True
    assert report["final_test"]["observations"] > 0
    assert report["walk_forward_oos"]["status"] == "complete"
    assert len(report["walk_forward_oos"]["folds"]) == 3
    assert all(item["only_past_visible"] for item in report["walk_forward_oos"]["folds"])
    assert report["gate_oos_metrics"]["source"] == "walk_forward_oos.aggregate"
    assert report["walk_forward_oos"]["aggregate_oos_observations"] == len(
        report["walk_forward_oos"]["aggregate_returns_by_date"]
    )
    assert report["parameter_experiment_scope"]["end"] < report["final_test"]["start"]
    assert report["parameter_experiment_scope"]["overlaps_final_test"] is False
    assert report["baseline_contract"]["status"] == "missing"
    assert report["baselines"] == []
    assert report["ablation"]["status"] == "completed"
    assert len(report["ablation"]["items"]) == 4
    assert len(report["stress"]) == 3
    assert all(item["engine_rerun"] for item in report["stress"])
    transactions = read_research_artifact(result["id"], "transactions.json", tmp_path)
    equity_curve = read_research_artifact(result["id"], "equity_curve.json", tmp_path)
    performance = read_research_artifact(result["id"], "performance_metrics.json", tmp_path)
    attribution = read_research_artifact(result["id"], "attribution.json", tmp_path)
    run_config = read_research_artifact(result["id"], "run_config.json", tmp_path)
    manifest = read_research_artifact(result["id"], "manifest.json", tmp_path)
    hashes = read_research_artifact(result["id"], "hashes.json", tmp_path)
    assert transactions["count"] == len(transactions["records"])
    assert equity_curve["count"] == len(equity_curve["records"])
    assert {"win_rate", "profit_loss_ratio", "turnover"} <= performance.keys()
    assert {"year", "theme", "stock"} <= attribution.keys()
    assert run_config["parameters"]["snapshot_content_hash"] in manifest["data_version"]
    assert "run_research_package" in run_config["reproduction_command"]
    assert {"transactions.json", "equity_curve.json", "performance_metrics.json",
            "attribution.json", "run_config.json", "walk_forward.json",
            "stress_tests.json"} <= hashes["artifacts"].keys()
    concentration = next(item for item in gates["gates"] if item["id"] == "BT-008-CONCENTRATION")
    assert concentration["actual"] == {
        "year": attribution["year"]["max_absolute_share"],
        "theme": attribution["theme"]["max_absolute_share"],
        "stock": attribution["stock"]["max_absolute_share"],
    }


def test_canonical_research_claims_reach_gates_and_override_legacy_aliases(tmp_path):
    snapshot = DeterministicDemoProvider().load()
    snapshot.metadata.update({
        "point_in_time_verified": True,
        "production_data_authorized": True,
        "pit_verified": False,
        "authorized": False,
    })
    accepted = run_research_package(snapshot, tmp_path / "accepted", capital=100_000)
    accepted_gates = {item["id"]: item for item in accepted["gates"]}
    assert accepted_gates["DAT-PIT"]["status"] == "PASS"
    assert accepted_gates["DATA-LICENSE"]["status"] == "PASS"
    assert accepted["report"]["data_eligibility"]["source_fields"] == {
        "point_in_time_verified": "point_in_time_verified",
        "production_data_authorized": "production_data_authorized",
    }

    snapshot.metadata.update({
        "point_in_time_verified": False,
        "production_data_authorized": False,
        "pit_verified": True,
        "authorized": True,
        "authorization_scope": "internal-research",
    })
    rejected = run_research_package(snapshot, tmp_path / "rejected", capital=100_000)
    rejected_gates = {item["id"]: item for item in rejected["gates"]}
    assert rejected_gates["DAT-PIT"]["status"] == "FAIL"
    assert rejected_gates["DATA-LICENSE"]["status"] == "FAIL"


def test_explicit_legacy_research_claims_require_authorization_scope(tmp_path):
    snapshot = DeterministicDemoProvider().load()
    for field in ("point_in_time_verified", "production_data_authorized"):
        snapshot.metadata.pop(field, None)
    snapshot.metadata.update({"pit_verified": True, "authorized": True, "authorization_scope": ""})
    no_scope = run_research_package(snapshot, tmp_path / "no-scope", capital=100_000)
    no_scope_gates = {item["id"]: item for item in no_scope["gates"]}
    assert no_scope_gates["DAT-PIT"]["status"] == "PASS"
    assert no_scope_gates["DATA-LICENSE"]["status"] == "FAIL"

    snapshot.metadata["authorization_scope"] = "internal-research"
    scoped = run_research_package(snapshot, tmp_path / "scoped", capital=100_000)
    scoped_gates = {item["id"]: item for item in scoped["gates"]}
    assert scoped_gates["DAT-PIT"]["status"] == "PASS"
    assert scoped_gates["DATA-LICENSE"]["status"] == "PASS"


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


def test_unofficial_baseline_is_visible_but_cannot_count_or_pass_gate(tmp_path):
    snapshot = DeterministicDemoProvider().load()
    split = make_time_series_split(sorted({bar.day for bar in snapshot.bars}), embargo_observations=2)
    final_dates = [
        day.isoformat() for day in sorted({bar.day for bar in snapshot.bars})
        if day.isoformat() >= split.windows[-1].start
    ]
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
    assert report["baseline_contract"]["status"] == "unverified"
    assert len(report["baselines"]) == 3
    assert all(item["source"].startswith("explicit-fixture:") for item in report["baselines"])
    gate = next(item for item in result["gates"] if item["id"] == "BT-006-BASELINES")
    assert gate["status"] == "FAIL" and gate["actual"] == 2


def test_three_complete_official_pit_baselines_are_eligible(tmp_path):
    snapshot = DeterministicDemoProvider().load()
    split = make_time_series_split(sorted({bar.day for bar in snapshot.bars}), embargo_observations=2)
    final_dates = [
        day.isoformat() for day in sorted({bar.day for bar in snapshot.bars})
        if day.isoformat() >= split.windows[-1].start
    ]
    snapshot.metadata["research_baselines"] = {
        "schema_version": "research-baselines/v1",
        "series": {
            name: {
                "returns_by_date": {day: .001 for day in final_dates},
                "source": f"official-pit-fixture:{name}",
                "is_official_point_in_time": True,
            }
            for name in ("沪深300", "中证全指", "简单动量")
        },
    }
    result = run_research_package(snapshot, tmp_path, capital=100_000)
    assert result["report"]["baseline_contract"]["status"] == "available"
    gate = next(item for item in result["gates"] if item["id"] == "BT-006-BASELINES")
    assert gate["status"] == "PASS" and gate["actual"] == 3
