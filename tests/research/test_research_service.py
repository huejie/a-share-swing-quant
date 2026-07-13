from quant_system.providers import DeterministicDemoProvider
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
    report = read_research_artifact(result["id"], "research_report.json", tmp_path)
    assert len(report["capacity"]) == 4
    assert len(report["sensitivity"]) == 9
    assert report["final_test"]["isolated_from_parameter_selection"] is True
    assert report["final_test"]["observations"] > 0
    assert report["ablation"]["status"] == "completed"
    assert len(report["ablation"]["items"]) == 4
