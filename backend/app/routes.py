from __future__ import annotations

from datetime import date
from typing import Any

from flask import Blueprint, current_app, jsonify, request

from .connectors import ERPAuthenticationError, make_reader
from .demo import demo_report
from .item_matching import match_journal_items
from .profiles import load_profiles, public_profiles
from .reconciliation import _serialize_line, build_report
from .storage import detect_retroactive_changes, get_run, list_issues, list_items, list_runs, previous_snapshot_date_to, review_issue, save_run


api = Blueprint("api", __name__)


def _profiles() -> list[dict[str, Any]]:
    if current_app.config["DEMO_MODE"]:
        return [{"key": "demo", "name": "Demo company (fictional data)"}]
    return load_profiles(current_app.config["COMPANIES_CONFIG"])


@api.get("/health")
def health() -> tuple:
    return jsonify({"status": "ok"}), 200


@api.get("/companies")
def companies() -> tuple:
    try:
        return jsonify({"companies": public_profiles(_profiles())}), 200
    except (OSError, ValueError) as error:
        return jsonify({"error": str(error)}), 503


@api.get("/runs")
def runs() -> tuple:
    return jsonify({"runs": list_runs(current_app.config["DATABASE"])}), 200


@api.post("/runs")
def create_run() -> tuple:
    payload = request.get_json(silent=True) or {}
    try:
        company_key = str(payload.get("company") or "")
        date_from = date.fromisoformat(str(payload.get("date_from") or ""))
        date_to = date.fromisoformat(str(payload.get("date_to") or ""))
        state_filter = str(payload.get("state_filter") or "posted")
        scope = str(payload.get("scope") or "company")
        if date_from > date_to:
            raise ValueError("Start date must not be after end date.")
        profile = next(
            (item for item in _profiles() if item["key"] == company_key), None
        )
        if profile is None:
            raise ValueError("Select a configured company.")
        previous_date = previous_snapshot_date_to(
            current_app.config["DATABASE"],
            company_key,
            date_from.isoformat(),
            date_to.isoformat(),
            state_filter,
            scope,
        )
        previous_date_to = date.fromisoformat(previous_date) if previous_date else None
        if company_key == "demo":
            report = demo_report(date_from, date_to, state_filter, scope)
        else:
            report = build_report(
                profile,
                date_from,
                date_to,
                state_filter,
                scope,
                comparison_date_to=previous_date_to,
            )
        report["issues"].extend(detect_retroactive_changes(current_app.config["DATABASE"], report))
        report["totals"]["issue_count"] = len(report["issues"])
        return jsonify(save_run(current_app.config["DATABASE"], report)), 201
    except ERPAuthenticationError as error:
        current_app.logger.warning(
            "ERP authentication rejected system=%s endpoint=%s database=%s username=%s",
            error.system,
            error.endpoint,
            error.database,
            error.username,
        )
        return jsonify({"error": str(error)}), 400
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except Exception:
        current_app.logger.exception("Accounting reconciliation run failed")
        return jsonify({"error": "Could not read both accounting systems. Check connectivity and read-only credentials."}), 502


@api.get("/runs/<run_id>")
def run_detail(run_id: str) -> tuple:
    report = get_run(current_app.config["DATABASE"], run_id)
    if report is None:
        return jsonify({"error": "Run not found."}), 404
    report["issues"] = [
        {
            "id": issue["id"],
            "type": issue["issue_type"],
            "account_code": issue["account_code"],
            "source_model": issue["source_model"],
            "source_id": issue["source_id"],
            "title": issue["title"],
            "details": issue["details"],
            "state": issue["state"],
            "assigned_to": issue["assigned_to"],
            "comments": issue["comments"],
        }
        for issue in list_issues(current_app.config["DATABASE"], run_id)
    ]
    return jsonify(report), 200


@api.get("/runs/<run_id>/issues")
def run_issues(run_id: str) -> tuple:
    if get_run(current_app.config["DATABASE"], run_id) is None:
        return jsonify({"error": "Run not found."}), 404
    return jsonify({"issues": list_issues(current_app.config["DATABASE"], run_id)}), 200


def _all_saved_items(run_id: str, account_code: str, system: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    offset = 0
    while True:
        page, total = list_items(
            current_app.config["DATABASE"],
            run_id,
            account_code,
            system,
            offset=offset,
            limit=500,
        )
        if not page:
            return items
        items.extend(page)
        offset += len(page)
        if offset >= total:
            return items


@api.get("/runs/<run_id>/accounts/<account_code>/items/comparison")
def account_item_comparison(run_id: str, account_code: str) -> tuple:
    report = get_run(current_app.config["DATABASE"], run_id)
    if report is None:
        return jsonify({"error": "Run not found."}), 404
    account_row = next(
        (item for item in report["accounts"] if item["account_code"] == account_code),
        None,
    )
    if account_row is None:
        return jsonify({"error": "Account not found in this run."}), 404

    try:
        source_items = _all_saved_items(run_id, account_code, "source")
        target_items = _all_saved_items(run_id, account_code, "target")
        if report.get("details_mode") == "live" and (not source_items or not target_items):
            profile = next(
                (item for item in _profiles() if item["key"] == report["company"]["key"]),
                None,
            )
            if profile is not None and profile.get("source") and profile.get("target"):
                date_from = date.fromisoformat(report["date_from"])
                date_to = date.fromisoformat(report["date_to"])
                if not source_items and account_row.get("source_account_codes"):
                    source = make_reader(profile["source"])
                    source_lines = source.journal_items(
                        int(profile["source"]["company_id"]),
                        date_from,
                        date_to,
                        report["state_filter"],
                        account_codes=set(account_row["source_account_codes"]),
                    )
                    source_items = [
                        {**_serialize_line(line), "mapped_account_code": account_code}
                        for line in source_lines
                    ]
                if not target_items:
                    target = make_reader(profile["target"])
                    target_lines = target.journal_items(
                        int(profile["target"]["company_id"]),
                        date_from,
                        date_to,
                        report["state_filter"],
                        account_codes={account_code},
                    )
                    target_items = [_serialize_line(line) for line in target_lines]

        source_items, target_items, matched_count = match_journal_items(
            source_items, target_items
        )
        include_matched = request.args.get("include_matched", "false").lower() in {
            "1", "true", "yes",
        }
        if not include_matched:
            source_items = [item for item in source_items if not item["matched"]]
            target_items = [item for item in target_items if not item["matched"]]
        source_total = len(source_items)
        target_total = len(target_items)
        return jsonify(
            {
                "source_items": source_items,
                "target_items": target_items,
                "source_total": source_total,
                "target_total": target_total,
                "matched_count": matched_count,
            }
        ), 200
    except ERPAuthenticationError as error:
        current_app.logger.warning(
            "ERP authentication rejected system=%s endpoint=%s database=%s username=%s",
            error.system,
            error.endpoint,
            error.database,
            error.username,
        )
        return jsonify({"error": str(error)}), 400
    except ValueError as error:
        return jsonify({"error": str(error)}), 400


@api.get("/runs/<run_id>/accounts/<account_code>/items")
def account_items(run_id: str, account_code: str) -> tuple:
    report = get_run(current_app.config["DATABASE"], run_id)
    if report is None:
        return jsonify({"error": "Run not found."}), 404
    try:
        system = request.args.get("system", "source")
        offset = max(int(request.args.get("offset", "0")), 0)
        limit = int(request.args.get("limit", "100"))
        items, total = list_items(
            current_app.config["DATABASE"],
            run_id,
            account_code,
            system,
            offset=offset,
            limit=limit,
        )
        if total == 0 and report.get("details_mode") == "live":
            profile = next(
                (item for item in _profiles() if item["key"] == report["company"]["key"]),
                None,
            )
            account_row = next(
                (item for item in report["accounts"] if item["account_code"] == account_code),
                None,
            )
            if profile is None or account_row is None:
                return jsonify({"items": [], "offset": offset, "limit": limit, "total": 0}), 200
            config = profile["source"] if system == "source" else profile["target"]
            reader = make_reader(config)
            account_filter = (
                set(account_row.get("source_account_codes") or [])
                if system == "source"
                else {account_code}
            )
            if not account_filter:
                return jsonify({"items": [], "offset": offset, "limit": limit, "total": 0}), 200
            lines = reader.journal_items(
                int(config["company_id"]),
                date.fromisoformat(report["date_from"]),
                date.fromisoformat(report["date_to"]),
                report["state_filter"],
                account_codes=account_filter,
            )
            items = [_serialize_line(line) for line in lines]
            total = len(items)
            items = items[offset:offset + min(max(limit, 1), 500)]
        return jsonify({"items": items, "offset": offset, "limit": min(max(limit, 1), 500), "total": total}), 200
    except ValueError as error:
        return jsonify({"error": str(error)}), 400


@api.patch("/issues/<issue_id>")
def update_issue(issue_id: str) -> tuple:
    payload = request.get_json(silent=True) or {}
    try:
        updated = review_issue(
            current_app.config["DATABASE"],
            issue_id,
            state=payload.get("state"),
            assigned_to=payload.get("assigned_to"),
            comment=payload.get("comment"),
            author=str(payload.get("author") or "accounting"),
        )
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    if not updated:
        return jsonify({"error": "Issue not found."}), 404
    return jsonify({"ok": True}), 200
