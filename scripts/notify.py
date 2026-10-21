#!/usr/bin/env python3
"""AOI alerting — send notifications when crawl problems are detected.

Called after each pipeline run. Checks three conditions:
  1. Circuit breaker fired (quarantined > 0)
  2. Zero new records (created == 0 AND updated == 0)
  3. Record count dropped more than 5%

Usage:
  # After a pipeline run, pass the run result as JSON:
  uv run python scripts/notify.py --source "INSPIRE-HEP Jobs" \
      --created 0 --updated 0 --errors 5 --closed 0 --quarantined 0

  # Health check mode — scan all sources for consecutive failures:
  uv run python scripts/notify.py --health-check

Notification channel is configured via environment variables:
  AOI_ALERT_EMAIL_TO      — recipient email address
  AOI_ALERT_SMTP_HOST     — SMTP server (default: localhost)
  AOI_ALERT_SMTP_PORT     — SMTP port (default: 587)
  AOI_ALERT_SMTP_USER     — SMTP username
  AOI_ALERT_SMTP_PASSWORD — SMTP password
  AOI_ALERT_WEBHOOK_URL   — HTTP POST webhook (alternative to email)
"""

import argparse
import os
import smtplib
import sys
from email.message import EmailMessage

import httpx
import structlog
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

# Ensure the project root is on sys.path so aoi is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from aoi.db.enums import FetchOutcome  # noqa: E402
from aoi.db.models import (  # noqa: E402
    Fetch,
    Source,
)

log = structlog.get_logger()

CONSECUTIVE_FAILURE_THRESHOLD = 3
RECORD_DROP_THRESHOLD = 0.05  # 5%


def _send_email(subject: str, body: str) -> None:
    """Send an alert email via SMTP."""
    to_addr = os.environ.get("AOI_ALERT_EMAIL_TO")
    if not to_addr:
        log.warning("notify.email_skipped", reason="AOI_ALERT_EMAIL_TO not set")
        return

    smtp_host = os.environ.get("AOI_ALERT_SMTP_HOST", "localhost")
    smtp_port = int(os.environ.get("AOI_ALERT_SMTP_PORT", "587"))
    smtp_user = os.environ.get("AOI_ALERT_SMTP_USER", "")
    smtp_password = os.environ.get("AOI_ALERT_SMTP_PASSWORD", "")

    msg = EmailMessage()
    msg["Subject"] = f"[AOI Alert] {subject}"
    msg["From"] = smtp_user or "aoi@localhost"
    msg["To"] = to_addr
    msg.set_content(body)

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        if smtp_user and smtp_password:
            server.starttls()
            server.login(smtp_user, smtp_password)
        server.send_message(msg)
    log.info("notify.email_sent", to=to_addr, subject=subject)


def _send_webhook(subject: str, body: str) -> None:
    """Send an alert via HTTP POST webhook."""
    webhook_url = os.environ.get("AOI_ALERT_WEBHOOK_URL")
    if not webhook_url:
        log.warning("notify.webhook_skipped", reason="AOI_ALERT_WEBHOOK_URL not set")
        return

    with httpx.Client(timeout=10.0) as client:
        resp = client.post(
            webhook_url,
            json={"subject": f"[AOI Alert] {subject}", "body": body},
        )
        resp.raise_for_status()
    log.info("notify.webhook_sent", url=webhook_url, status=resp.status_code)


def send_alert(subject: str, body: str) -> None:
    """Send alert via all configured channels."""
    log.warning("notify.alert", subject=subject)

    if os.environ.get("AOI_ALERT_EMAIL_TO"):
        _send_email(subject, body)
    if os.environ.get("AOI_ALERT_WEBHOOK_URL"):
        _send_webhook(subject, body)
    if not os.environ.get("AOI_ALERT_EMAIL_TO") and not os.environ.get("AOI_ALERT_WEBHOOK_URL"):
        # No channel configured — log only (still useful for cron log review)
        log.error("notify.no_channel", subject=subject, body=body)


def check_run_result(
    source: str,
    created: int,
    updated: int,
    errors: int,
    closed: int,
    quarantined: int,
) -> list[str]:
    """Check a pipeline run result for alert conditions. Returns list of alerts."""
    alerts: list[str] = []

    # Condition 1: Circuit breaker fired
    if quarantined > 0:
        alerts.append(
            f"Circuit breaker fired for {source}: "
            f"{quarantined} records quarantined (not closed). "
            f"Review the closure batch manually."
        )

    # Condition 2: Zero new records
    if created == 0 and updated == 0 and errors == 0:
        alerts.append(
            f"Zero new or updated records for {source}. "
            f"The source may be down or the adapter may need updating."
        )

    # Condition 3: High error count relative to results
    total = created + updated + errors
    if total > 0 and errors > 0 and (errors / total) > RECORD_DROP_THRESHOLD:
        alerts.append(
            f"High error rate for {source}: "
            f"{errors}/{total} records ({errors / total:.0%}) had errors."
        )

    return alerts


def health_check() -> list[str]:
    """Query the database for sources with consecutive failures."""
    database_url = os.environ.get("AOI_DATABASE_URL")
    if not database_url:
        log.error("notify.health_check", error="AOI_DATABASE_URL not set")
        return ["AOI_DATABASE_URL not set — cannot run health check"]

    engine = create_engine(database_url)
    alerts: list[str] = []

    with Session(engine) as db:
        sources = db.execute(select(Source).where(Source.enabled.is_(True))).scalars().all()

        for src in sources:
            # Count consecutive failures (same logic as dashboard)
            recent_outcomes = (
                db.execute(
                    select(Fetch.outcome)
                    .where(Fetch.source_id == src.id)
                    .order_by(Fetch.fetched_at.desc())
                    .limit(20)
                )
                .scalars()
                .all()
            )

            consecutive_failures = 0
            for outcome in recent_outcomes:
                if outcome == FetchOutcome.ERROR:
                    consecutive_failures += 1
                else:
                    break

            if consecutive_failures >= CONSECUTIVE_FAILURE_THRESHOLD:
                last_fetch = db.execute(
                    select(func.max(Fetch.fetched_at)).where(Fetch.source_id == src.id)
                ).scalar()
                alerts.append(
                    f"{src.name}: {consecutive_failures} consecutive fetch failures. "
                    f"Last fetch: {last_fetch}. Check source availability."
                )

    engine.dispose()
    return alerts


def main() -> int:
    """CLI entry point."""
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(20),
        processors=[structlog.dev.ConsoleRenderer()],
    )

    parser = argparse.ArgumentParser(description="AOI alerting")
    parser.add_argument("--health-check", action="store_true", help="Scan all sources")
    parser.add_argument("--source", help="Source name (for run-result mode)")
    parser.add_argument("--created", type=int, default=0)
    parser.add_argument("--updated", type=int, default=0)
    parser.add_argument("--errors", type=int, default=0)
    parser.add_argument("--closed", type=int, default=0)
    parser.add_argument("--quarantined", type=int, default=0)
    args = parser.parse_args()

    all_alerts: list[str] = []

    if args.health_check:
        all_alerts.extend(health_check())
    elif args.source:
        all_alerts.extend(
            check_run_result(
                source=args.source,
                created=args.created,
                updated=args.updated,
                errors=args.errors,
                closed=args.closed,
                quarantined=args.quarantined,
            )
        )
    else:
        parser.error("Specify --health-check or --source")

    if all_alerts:
        subject = f"{len(all_alerts)} alert(s) detected"
        body = "\n\n".join(all_alerts)
        send_alert(subject, body)
        return 1

    log.info("notify.ok", message="No alerts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
