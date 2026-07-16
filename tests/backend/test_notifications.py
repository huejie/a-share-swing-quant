from __future__ import annotations

import json
from datetime import date

from fastapi.testclient import TestClient

from apps.api.main import app, service as api_service
from quant_system.notifications import NotificationDispatcher
from quant_system.providers import DeterministicDemoProvider
from quant_system.repository import SQLiteRepository
from quant_system.service import QuantService, Settings


class _Response:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_none_channel_is_audited_without_external_delivery(tmp_path, monkeypatch):
    repository = SQLiteRepository(tmp_path / "none.db")
    called = False

    def unexpected(*_, **__):
        nonlocal called
        called = True
        raise AssertionError("none channel must not use the network")

    monkeypatch.setattr("quant_system.notifications.urlopen", unexpected)
    item = NotificationDispatcher(repository, {}).emit(
        event_key="run-1:eod_success", event_type="eod_success", channel="none",
        payload={"run_key": "run-1", "message": "ok"},
    )
    assert item["status"] == "skipped" and item["attempts"] == 1
    assert called is False


def test_webhook_success_is_sent_and_event_key_is_idempotent(tmp_path, monkeypatch):
    repository = SQLiteRepository(tmp_path / "sent.db")
    calls = []

    def accepted(request, timeout):
        calls.append((request, timeout))
        return _Response()

    monkeypatch.setattr("quant_system.notifications.urlopen", accepted)
    dispatcher = NotificationDispatcher(repository, {
        "QUANT_NOTIFICATION_WEBHOOK_URL": "https://notify.invalid/endpoint",
    })
    first = dispatcher.emit(
        event_key="run-2:eod_success", event_type="eod_success", channel="webhook",
        payload={"run_key": "run-2", "decision_id": "decision-2"},
    )
    second = dispatcher.emit(
        event_key="run-2:eod_success", event_type="eod_success", channel="webhook",
        payload={"run_key": "run-2", "decision_id": "decision-2"},
    )
    assert first["status"] == "sent" and first["sent_at"]
    assert second["id"] == first["id"] and second["attempts"] == 1
    assert len(calls) == 1


def test_delivery_failure_and_api_never_expose_secrets(tmp_path, monkeypatch):
    repository = SQLiteRepository(tmp_path / "failure.db")
    secret_url = "https://user:TOP-SECRET@notify.invalid/private"

    def failed(*_, **__):
        raise OSError(f"could not reach {secret_url}")

    monkeypatch.setattr("quant_system.notifications.urlopen", failed)
    item = NotificationDispatcher(repository, {
        "QUANT_NOTIFICATION_WEBHOOK_URL": secret_url,
        "QUANT_NOTIFICATION_SMTP_PASSWORD": "SMTP-TOP-SECRET",
    }).emit(
        event_key="run-3:risk_alert", event_type="risk_alert", channel="webhook",
        payload={"run_key": "run-3", "message": "risk", "password": "PAYLOAD-SECRET"},
    )
    assert item["status"] == "failed"
    assert item["last_error"] == "delivery_failed:OSError"

    raw = (tmp_path / "failure.db").read_bytes()
    assert b"TOP-SECRET" not in raw and b"PAYLOAD-SECRET" not in raw

    monkeypatch.setattr(api_service, "repository", repository)
    monkeypatch.setenv("QUANT_ADMIN_API_KEY", "admin-only-secret")
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/api/v1/notifications").status_code == 401
    response = client.get("/api/v1/notifications", headers={"X-Admin-Key": "admin-only-secret"})
    assert response.status_code == 200
    encoded = json.dumps(response.json())
    assert "TOP-SECRET" not in encoded and "PAYLOAD-SECRET" not in encoded


def test_failed_notification_can_be_retried_after_configuration_recovers(tmp_path, monkeypatch):
    repository = SQLiteRepository(tmp_path / "retry.db")
    environment: dict[str, str] = {}
    dispatcher = NotificationDispatcher(repository, environment)
    failed = dispatcher.emit(
        event_key="run-4:eod_success", event_type="eod_success", channel="webhook",
        payload={"run_key": "run-4"},
    )
    assert failed["status"] == "failed" and failed["last_error"] == "configuration_missing"

    environment["QUANT_NOTIFICATION_WEBHOOK_URL"] = "https://notify.invalid/recovered"
    monkeypatch.setattr("quant_system.notifications.urlopen", lambda *_, **__: _Response())
    sent = dispatcher.retry(failed["id"])
    assert sent["status"] == "sent" and sent["attempts"] == 2 and sent["last_error"] is None


def test_email_delivery_uses_environment_only(tmp_path, monkeypatch):
    repository = SQLiteRepository(tmp_path / "email.db")
    events = []

    class SMTP:
        def __init__(self, host, port, timeout):
            events.append(("connect", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def starttls(self):
            events.append(("starttls",))

        def login(self, username, password):
            events.append(("login", username, password))

        def send_message(self, message):
            events.append(("send", message["To"]))

    monkeypatch.setattr("quant_system.notifications.smtplib.SMTP", SMTP)
    item = NotificationDispatcher(repository, {
        "QUANT_NOTIFICATION_SMTP_HOST": "smtp.private.invalid",
        "QUANT_NOTIFICATION_SMTP_USERNAME": "PRIVATE-USER",
        "QUANT_NOTIFICATION_SMTP_PASSWORD": "PRIVATE-PASSWORD",
        "QUANT_NOTIFICATION_EMAIL_FROM": "private-from@example.invalid",
        "QUANT_NOTIFICATION_EMAIL_TO": "private-to@example.invalid",
    }).emit(
        event_key="run-email:eod_success", event_type="eod_success", channel="email",
        payload={"run_key": "run-email", "message": "ok"},
    )
    assert item["status"] == "sent" and any(event[0] == "send" for event in events)
    encoded = json.dumps(repository.list_notifications())
    assert "PRIVATE-USER" not in encoded and "PRIVATE-PASSWORD" not in encoded
    assert "private-to@example.invalid" not in encoded and "smtp.private.invalid" not in encoded


def test_eod_notifications_are_durable_and_failure_does_not_fail_decision(tmp_path, monkeypatch):
    repository = SQLiteRepository(tmp_path / "eod.db")
    settings = Settings(notification_channel="webhook", notify_eod_success=True, notify_risk=True)
    quant = QuantService(provider=DeterministicDemoProvider(), settings=settings, repository=repository)
    monkeypatch.setenv("QUANT_NOTIFICATION_WEBHOOK_URL", "https://notify.invalid/private-token")
    monkeypatch.setattr(
        "quant_system.notifications.urlopen",
        lambda *_, **__: (_ for _ in ()).throw(TimeoutError("private-token")),
    )

    result = quant.run_eod(date(2026, 7, 3), run_key="notification-eod-success")
    assert result["displayable"] is True
    success = repository.list_notifications(event_type="eod_success")
    assert len(success) == 1 and success[0]["status"] == "failed"

    blocked = quant.run_eod(
        date(2025, 1, 3), enforce_freshness=True, run_key="notification-quality-blocked"
    )
    assert blocked["published"] is False
    quality = repository.list_notifications(event_type="quality_blocked")
    assert len(quality) == 1 and quality[0]["status"] == "failed"

    replay = quant.run_eod(date(2026, 7, 3), run_key="notification-eod-success")
    assert replay["idempotent_replay"] is True
    assert len(repository.list_notifications(event_type="eod_success")) == 1


def test_priority_exit_emits_risk_notification_even_when_portfolio_is_not_market_risk_off(tmp_path, monkeypatch):
    repository = SQLiteRepository(tmp_path / "priority-exit.db")
    quant = QuantService(
        repository=repository,
        settings=Settings(notification_channel="none", notify_eod_success=False, notify_risk=True),
    )
    monkeypatch.setattr(
        quant,
        "_exit_actions",
        lambda *_: [{"symbol": "RISK.SH", "exit_priority": 1, "exit_reason": "硬风险"}],
    )

    result = quant.run_eod(date(2026, 7, 3), run_key="notification-priority-exit")

    assert result["portfolio_condition"] == "healthy"
    alerts = repository.list_notifications(event_type="risk_alert")
    assert len(alerts) == 1
    assert alerts[0]["status"] == "skipped"
    assert "退出条件" in alerts[0]["payload"]["message"]
