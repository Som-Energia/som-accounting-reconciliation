from __future__ import annotations

from datetime import date
from typing import Any

from flask import Blueprint, current_app, jsonify, request

from .demo import demo_report
from .profiles import load_profiles, public_profiles
from .reconciliation import build_report
from .storage import detect_retroactive_changes, get_run, list_issues, list_items, list_runs, review_issue, save_run


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
        if company_key == "demo":
            report = demo_report(date_from, date_to, state_filter, scope)
        else:
            report = build_report(profile, date_from, date_to, state_filter, scope)
        report["issues"].extend(detect_retroactive_changes(current_app.config["DATABASE"], report))
        report["totals"]["issue_count"] = len(report["issues"])
        return jsonify(save_run(current_app.config["DATABASE"], report)), 201
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


@api.get("/runs/<run_id>/accounts/<account_code>/items")
def account_items(run_id: str, account_code: str) -> tuple:
    if get_run(current_app.config["DATABASE"], run_id) is None:
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
