from __future__ import annotations

import argparse
from datetime import date
import json
import os
import calendar
from datetime import timedelta
from pathlib import Path

from .profiles import load_profiles
from .reconciliation import build_report
from .storage import detect_retroactive_changes, initialize, previous_snapshot_date_to, purge_expired, save_run


def _subtract_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 - months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run read-only accounting reconciliation")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--company", required=True)
    run_parser.add_argument("--date-from", required=True)
    run_parser.add_argument("--date-to", required=True)
    run_parser.add_argument("--state", choices=("posted", "all"), default="posted")
    run_parser.add_argument("--scope", choices=("company", "synced"), default="company")
    purge_parser = subparsers.add_parser("purge-expired")
    purge_parser.add_argument("--today", default=date.today().isoformat())
    scheduled_parser = subparsers.add_parser("scheduled")
    scheduled_parser.add_argument("--kind", choices=("nightly", "monthly"), required=True)
    scheduled_parser.add_argument("--today", default=date.today().isoformat())
    args = parser.parse_args()

    database = os.environ.get(
        "RECONCILIATION_DATABASE",
        str(Path(__file__).resolve().parents[1] / "instance" / "reconciliation.sqlite3"),
    )
    initialize(database)
    if args.command == "purge-expired":
        print(json.dumps({"deleted_runs": purge_expired(database, today=args.today)}))
        return

    profiles = load_profiles(os.environ.get("COMPANIES_CONFIG", ""))
    if args.command == "scheduled":
        today = date.fromisoformat(args.today)
        date_to = today - timedelta(days=1)
        reports = []
        for profile in profiles:
            if args.kind == "nightly":
                date_from = _subtract_months(today, 3)
            else:
                date_from = date.fromisoformat(profile.get("history_start", "1900-01-01"))
            previous = previous_snapshot_date_to(
                database,
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
            report["issues"].extend(detect_retroactive_changes(database, report))
            report["totals"]["issue_count"] = len(report["issues"])
            saved = save_run(database, report)
            reports.append({"company": profile["key"], "run_id": saved["id"]})
        purge_count = purge_expired(database, today=today.isoformat())
        print(json.dumps({"kind": args.kind, "runs": reports, "deleted_runs": purge_count}))
        return

    profile = next((item for item in profiles if item["key"] == args.company), None)
    if profile is None:
        raise SystemExit(f"Unknown company profile: {args.company}")
    previous = previous_snapshot_date_to(
        database,
        profile["key"],
        args.date_from,
        args.date_to,
        args.state,
        args.scope,
    )
    report = build_report(
        profile,
        date.fromisoformat(args.date_from),
        date.fromisoformat(args.date_to),
        args.state,
        args.scope,
        comparison_date_to=date.fromisoformat(previous) if previous else None,
    )
    report["issues"].extend(detect_retroactive_changes(database, report))
    report["totals"]["issue_count"] = len(report["issues"])
    saved = save_run(database, report)
    print(json.dumps({"run_id": saved["id"], "totals": saved["totals"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
