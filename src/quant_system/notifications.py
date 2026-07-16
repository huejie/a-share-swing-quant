from __future__ import annotations

import json
import os
import smtplib
from email.message import EmailMessage
from typing import Mapping
from urllib.request import Request, urlopen
from uuid import uuid4

from .repository import SQLiteRepository


class NotificationDispatcher:
    """Secret-free durable notification outbox with best-effort delivery.

    Delivery targets and credentials deliberately have no constructor fields
    and are resolved from the process environment only at delivery time.
    """

    def __init__(self, repository: SQLiteRepository, environ: Mapping[str, str] | None = None):
        self.repository = repository
        self._environ = environ if environ is not None else os.environ

    def emit(self, *, event_key: str, event_type: str, channel: str, payload: dict) -> dict:
        if channel not in {"none", "webhook", "email"}:
            channel = "none"
        item = self.repository.enqueue_notification(
            str(uuid4()), event_key, event_type, channel, self._safe_payload(payload)
        )
        # An idempotent replay returns its terminal record without redelivery.
        if item["status"] != "pending":
            return item
        return self._deliver(item, from_status="pending")

    def retry(self, ident: str) -> dict | None:
        item = self.repository.get_notification(ident)
        if item is None:
            return None
        if item["status"] != "failed":
            raise ValueError("only failed notifications can be retried")
        return self._deliver(item, from_status="failed")

    def _deliver(self, item: dict, *, from_status: str) -> dict:
        claimed = self.repository.claim_notification(item["id"], from_status)
        if claimed is None:
            # Another process already claimed this idempotent event.
            return self.repository.get_notification(item["id"])
        item = claimed
        if item["channel"] == "none":
            return self.repository.finish_notification(item["id"], "skipped")
        try:
            if item["channel"] == "webhook":
                self._send_webhook(item)
            elif item["channel"] == "email":
                self._send_email(item)
            else:
                raise RuntimeError("unsupported_channel")
        except Exception as exc:  # delivery is intentionally isolated from EOD
            return self.repository.finish_notification(
                item["id"], "failed", error=self._safe_error(exc)
            )
        return self.repository.finish_notification(item["id"], "sent")

    def _send_webhook(self, item: dict) -> None:
        url = self._environ.get("QUANT_NOTIFICATION_WEBHOOK_URL", "").strip()
        if not url:
            raise RuntimeError("configuration_missing")
        body = json.dumps(
            {"event_type": item["event_type"], "created_at": item["created_at"], "payload": item["payload"]},
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": "a-share-quant/notification"})
        with urlopen(request, timeout=self._timeout()) as response:  # nosec B310: URL is an operator-owned env setting
            if not 200 <= int(response.status) < 300:
                raise RuntimeError("remote_rejected")

    def _send_email(self, item: dict) -> None:
        env = self._environ
        host = env.get("QUANT_NOTIFICATION_SMTP_HOST", "").strip()
        sender = env.get("QUANT_NOTIFICATION_EMAIL_FROM", "").strip()
        recipients = [value.strip() for value in env.get("QUANT_NOTIFICATION_EMAIL_TO", "").split(",") if value.strip()]
        if not host or not sender or not recipients:
            raise RuntimeError("configuration_missing")
        try:
            port = int(env.get("QUANT_NOTIFICATION_SMTP_PORT", "587"))
        except ValueError as exc:
            raise RuntimeError("configuration_invalid") from exc
        message = EmailMessage()
        message["Subject"] = f"A股量化系统通知：{item['event_type']}"
        message["From"] = sender
        message["To"] = ", ".join(recipients)
        message.set_content(json.dumps(item["payload"], ensure_ascii=False, indent=2))
        with smtplib.SMTP(host, port, timeout=self._timeout()) as smtp:
            if env.get("QUANT_NOTIFICATION_SMTP_STARTTLS", "true").strip().lower() not in {"0", "false", "no"}:
                smtp.starttls()
            username = env.get("QUANT_NOTIFICATION_SMTP_USERNAME", "")
            password = env.get("QUANT_NOTIFICATION_SMTP_PASSWORD", "")
            if username or password:
                if not username or not password:
                    raise RuntimeError("configuration_incomplete")
                smtp.login(username, password)
            smtp.send_message(message)

    def _timeout(self) -> float:
        try:
            return min(30.0, max(1.0, float(self._environ.get("QUANT_NOTIFICATION_TIMEOUT_SECONDS", "8"))))
        except ValueError:
            return 8.0

    @staticmethod
    def _safe_payload(payload: dict) -> dict:
        """Whitelist event facts so arbitrary caller data cannot leak credentials."""
        allowed = {
            "run_key", "decision_id", "as_of", "model_version", "provider", "release_mode",
            "quality_status", "quality_issue_codes", "portfolio_status", "portfolio_condition",
            "market_regime", "market_exposure_cap", "message",
        }
        return {key: value for key, value in payload.items() if key in allowed}

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        # Never persist exception text: urllib/SMTP errors may echo a target,
        # username, server response, or credentials. Only closed safe codes.
        message = str(exc)
        if isinstance(exc, RuntimeError) and message in {
            "configuration_missing", "configuration_invalid", "configuration_incomplete",
            "remote_rejected", "unsupported_channel",
        }:
            return message
        return f"delivery_failed:{type(exc).__name__}"
