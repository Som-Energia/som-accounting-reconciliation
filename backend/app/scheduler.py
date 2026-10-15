from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import logging
import os
import time
from pathlib import Path

from .profiles import load_profiles
from .reconciliation import build_report
from .storage import claim_schedule, detect_retroactive_changes, finish_schedule, initialize, previous_snapshot_date_to, purge_expired, save_run


LOGGER = logging.getLogger("accounting_reconciliation.scheduler")
DATABASE = os.environ.get(
    "RECONCILIATION_DATABASE",
    str(Path(__file__).resolve().parents[1] / "instance" / "reconciliation.sqlite3"),
)


def _subtract_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 - months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    import calendar

    return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))


def execute_scheduled(kind: str, today: date) -> None:
    profiles = load_profiles(os.environ.get("COMPANIES_CONFIG", ""))
    date_to = today - timedelta(days=1)
    for profile in profiles:
        job_key = f"{kind}:{profile['key']}:{today.isoformat()}"
        if not claim_schedule(DATABASE, job_key):
            continue
        try:
            date_from = (
                _subtract_months(today, 3)
                if kind == "nightly"
                else date.fromisoformat(profile.get("history_start", "1900-01-01"))
            )
            previous = previous_snapshot_date_to(
                DATABASE,
                profile["key"],
                date_from.isoformat(),
                date_to.isoformat(),
                "posted",
                "company",
            )
            report = build_report(
                profile,
                date_from,
                date_to,
                "posted",
                "company",
                comparison_date_to=date.fromisoformat(previous) if previous else None,
            )
            report["issues"].extend(detect_retroactive_changes(DATABASE, report))
            report["totals"]["issue_count"] = len(report["issues"])
            saved = save_run(DATABASE, report)
            finish_schedule(DATABASE, job_key)
            LOGGER.info("Completed %s reconciliation for %s as run %s", kind, profile["key"], saved["id"])
        except Exception as error:  # scheduled failures must not stop other company profiles
            finish_schedule(DATABASE, job_key, str(error)[:1000])
            LOGGER.exception("Scheduled %s reconciliation failed for %s", kind, profile["key"])
    purge_expired(DATABASE, today=today.isoformat())


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    initialize(DATABASE)
    LOGGER.info("Scheduler started; nightly at 02:00 UTC, full history at 04:00 UTC on day 1")
    last_tick: tuple[int, int, int, int, int] | None = None
    while True:
        now = datetime.now(timezone.utc)
        tick = (now.year, now.month, now.day, now.hour, now.minute)
        if tick != last_tick:
            last_tick = tick
            if now.hour == 2 and now.minute < 15:
                execute_scheduled("nightly", now.date())
            elif now.day == 1 and now.hour == 4 and now.minute < 15:
                execute_scheduled("monthly", now.date())
        time.sleep(15)


if __name__ == "__main__":
    main()
