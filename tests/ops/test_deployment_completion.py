"""Static acceptance checks for the deployed single-server topology."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_compose_keeps_api_private_and_product_data_on_a_named_volume():
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    api_block, web_block = compose.split("  web:", 1)
    assert "ports:" not in api_block
    assert "quant-data:/app/data" in api_block
    assert "ports:" in web_block
    assert "quant-data:" in compose


def test_weekday_eod_timer_is_versioned_with_the_deployment():
    timer = (ROOT / "deploy/systemd/a-share-quant-eod.timer").read_text(encoding="utf-8")
    service = (ROOT / "deploy/systemd/a-share-quant-eod.service").read_text(encoding="utf-8")
    deploy = (ROOT / "deploy/deploy.sh").read_text(encoding="utf-8")
    assert "OnCalendar=Mon..Fri" in timer and "Asia/Shanghai" in timer
    assert "Persistent=true" in timer
    assert "--config /etc/a-share-quant/eod.curl.conf" in service
    assert "/api/v1/pipeline/eod" in deploy
    assert 'header = "X-Admin-Key:' in deploy
    assert "chmod 0600 /etc/a-share-quant/eod.curl.conf" in deploy
    assert "X-Admin-Key" not in service
    assert "Restart=on-failure" in service


def test_daily_persistent_data_backup_is_deployed_not_only_documented():
    """NFR-005 requires an automated backup, not only a manual Windows script."""
    timer_path = ROOT / "deploy/systemd/a-share-quant-backup.timer"
    service_path = ROOT / "deploy/systemd/a-share-quant-backup.service"
    assert timer_path.is_file() and service_path.is_file()
    timer = timer_path.read_text(encoding="utf-8")
    service = service_path.read_text(encoding="utf-8")
    assert "OnCalendar=" in timer and "Persistent=true" in timer
    assert "quant-data" in service or "/app/data" in service
