import json
import logging
from datetime import date
from decimal import Decimal

import pytest

from app import create_app
from app import routes
from app.connectors import ERPAuthenticationError
from app.demo import demo_report
from app.domain import JournalItem
from app.storage import claim_schedule, detect_retroactive_changes, finish_schedule, get_run, initialize, list_issues, list_items, purge_expired, review_issue, save_run


def test_saved_report_has_reviewable_issue_and_expires_after_two_years(tmp_path):
    database = str(tmp_path / "report.sqlite3")
    initialize(database)
    report = demo_report(date(2026, 1, 1), date(2026, 1, 31), "posted", "company")
    saved = save_run(database, report)
    issue = next(item for item in saved["issues"] if item["type"] == "pending_sync")
    assert review_issue(database, issue["id"], assigned_to="Accounting", comment="Check import")
    issue_row = next(item for item in list_issues(database, saved["id"]) if item["id"] == issue["id"])
    assert issue_row["assigned_to"] == "Accounting"
    assert issue_row["comments"][0]["body"] == "Check import"
    assert get_run(database, saved["id"])["accounts"]
    source_items, total = list_items(database, saved["id"], "430000", "source", limit=1)
    assert len(source_items) == 1
    assert total == 4
    assert purge_expired(database, today="2029-01-01") == 1
    assert get_run(database, saved["id"]) is None


def test_issue_assignment_and_comments_survive_repeated_snapshots(tmp_path):
    database = str(tmp_path / "issues.sqlite3")
    initialize(database)
    first = save_run(database, demo_report(date(2026, 1, 1), date(2026, 1, 31), "posted", "company"))
    old_issue = next(item for item in first["issues"] if item["type"] == "pending_sync")
    review_issue(database, old_issue["id"], state="reviewed", assigned_to="Finance", comment="Investigating")
    second = save_run(database, demo_report(date(2026, 1, 1), date(2026, 1, 31), "posted", "company"))
    new_issue = next(item for item in list_issues(database, second["id"]) if item["issue_type"] == "pending_sync")
    assert new_issue["id"] == old_issue["id"]
    assert new_issue["state"] == "reviewed"
    assert new_issue["assigned_to"] == "Finance"
    assert new_issue["comments"][0]["body"] == "Investigating"


def test_temporal_difference_reopens_as_incident_after_month_close(tmp_path):
    database = str(tmp_path / "ageing.sqlite3")
    initialize(database)
    first_report = demo_report(date(2026, 1, 1), date(2026, 1, 31), "posted", "company")
    first_mismatch = next(item for item in first_report["issues"] if item["type"] == "movement_difference")
    first_mismatch["type"] = "temporal_difference"
    first = save_run(database, first_report)
    issue_id = next(item["id"] for item in first["issues"] if item["type"] == "temporal_difference")
    review_issue(database, issue_id, state="resolved")

    second = save_run(database, demo_report(date(2026, 1, 1), date(2026, 1, 31), "posted", "company"))
    issue = next(item for item in list_issues(database, second["id"]) if item["id"] == issue_id)
    assert issue["issue_type"] == "movement_difference"
    assert issue["state"] == "open"


def test_demo_run_is_available_through_api_without_erp_credentials(tmp_path):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "api.sqlite3"), "DEMO_MODE": True})
    client = app.test_client()
    response = client.post(
        "/api/runs",
        json={
            "company": "demo",
            "date_from": "2026-01-01",
            "date_to": "2026-01-31",
            "state_filter": "posted",
            "scope": "company",
        },
    )
    assert response.status_code == 201
    run_id = response.json["id"]
    assert client.get(f"/api/runs/{run_id}").status_code == 200
    assert client.get(f"/api/runs/{run_id}/issues").status_code == 200
    detail = client.get(f"/api/runs/{run_id}/accounts/430000/items?system=source&limit=1")
    assert detail.status_code == 200
    assert detail.json["total"] == 4
    assert len(detail.json["items"]) == 1


def test_demo_account_comparison_hides_exact_matches_but_can_include_them(tmp_path):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "comparison.sqlite3"), "DEMO_MODE": True})
    client = app.test_client()
    run_response = client.post(
        "/api/runs",
        json={
            "company": "demo",
            "date_from": "2026-01-01",
            "date_to": "2026-01-31",
            "state_filter": "posted",
            "scope": "company",
        },
    )
    run_id = run_response.json["id"]

    response = client.get(f"/api/runs/{run_id}/accounts/430000/items/comparison")
    assert response.status_code == 200
    assert response.json["matched_count"] == 2
    assert response.json["source_total"] == 2
    assert response.json["target_total"] == 2
    assert {item["line_id"] for item in response.json["source_items"]} == {2002, 3001}
    assert {item["line_id"] for item in response.json["target_items"]} == {5021, 5030}

    all_items = client.get(
        f"/api/runs/{run_id}/accounts/430000/items/comparison?include_matched=true"
    )
    assert all_items.status_code == 200
    assert all_items.json["source_total"] == 4
    assert all_items.json["target_total"] == 4
    assert {item["line_id"] for item in all_items.json["source_items"] if item["matched"]} == {1001, 1010}
    assert {item["line_id"] for item in all_items.json["target_items"] if item["matched"]} == {5001, 5010}


def test_live_account_comparison_loads_both_erp_ledgers(tmp_path, monkeypatch):
    companies_config = tmp_path / "companies.json"
    companies_config.write_text(
        json.dumps(
            {
                "companies": [
                    {
                        "key": "example",
                        "name": "Example",
                        "source": {
                            "url": "https://openerp.example.test",
                            "database": "legacy",
                            "company_id": 1,
                            "username": "reader",
                            "protocol": "openerp5",
                        },
                        "target": {
                            "url": "https://odoo.example.test",
                            "database": "modern",
                            "company_id": 1,
                            "username": "reader",
                            "protocol": "odoo18",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    def journal_item(system, line_id, *, model="account.invoice", source_id=7, native=False):
        return JournalItem(
            system=system,
            line_id=line_id,
            move_id=line_id + 100,
            account_code="430000001" if system == "source" else "430000",
            account_name="Receivables",
            date=date(2026, 1, 15),
            debit=Decimal("100.00"),
            credit=Decimal("0.00"),
            source_model=model,
            source_id=source_id,
            is_native=native,
            partner_id=42,
        )

    source_lines = [journal_item("source", 1)]
    target_lines = [
        journal_item("target", 11),
        journal_item("target", 12, model="", source_id=None, native=True),
    ]

    class Reader:
        def __init__(self, lines):
            self.lines = lines

        def journal_items(self, *_args, **_kwargs):
            return self.lines

    monkeypatch.setattr(
        routes,
        "make_reader",
        lambda config: Reader(source_lines if config["protocol"] == "openerp5" else target_lines),
    )
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "live-comparison.sqlite3"),
            "COMPANIES_CONFIG": str(companies_config),
            "DEMO_MODE": False,
        }
    )
    report = {
        "company": {"key": "example", "name": "Example"},
        "date_from": "2026-01-01",
        "date_to": "2026-01-31",
        "state_filter": "posted",
        "scope": "company",
        "details_mode": "live",
        "accounts": [
            {
                "account_code": "430000",
                "source_account_codes": ["430000001"],
                "source_lines": [],
                "target_lines": [],
            }
        ],
        "issues": [],
        "totals": {},
    }
    run = save_run(str(tmp_path / "live-comparison.sqlite3"), report)

    response = app.test_client().get(
        f"/api/runs/{run['id']}/accounts/430000/items/comparison"
    )

    assert response.status_code == 200
    assert response.json["matched_count"] == 1
    assert response.json["source_items"] == []
    assert [item["line_id"] for item in response.json["target_items"]] == [12]
    assert response.json["target_items"][0]["partner_id"] == 42


def test_invalid_period_is_rejected(tmp_path):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "api.sqlite3"), "DEMO_MODE": True})
    response = app.test_client().post(
        "/api/runs",
        json={"company": "demo", "date_from": "2026-02-01", "date_to": "2026-01-01"},
    )
    assert response.status_code == 400


@pytest.mark.parametrize("system", ["OpenERP", "Odoo"])
def test_erp_authentication_failure_logs_provider_context_without_secrets(
    system, tmp_path, monkeypatch, caplog
):
    profile = {
        "key": "som-energia",
        "name": "Som Energia SCCL",
        "source": {},
        "target": {
            "url": "https://odoo.example.test/rpc?token=url-secret",
            "database": "odoo_db",
            "username": "reader",
        },
    }

    def fail_report(*_args, **_kwargs):
        raise ERPAuthenticationError(system, profile["target"])

    monkeypatch.setattr(routes, "_profiles", lambda: [profile])
    monkeypatch.setattr(routes, "build_report", fail_report)
    caplog.set_level(logging.WARNING)
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "api.sqlite3")})

    response = app.test_client().post(
        "/api/runs",
        json={
            "company": "som-energia",
            "date_from": "2026-01-01",
            "date_to": "2026-01-31",
            "state_filter": "posted",
            "scope": "company",
        },
    )

    assert response.status_code == 400
    assert response.json == {
        "error": f"{system} authentication failed for the configured read-only user."
    }
    assert f"system={system}" in caplog.text
    assert "endpoint=https://odoo.example.test" in caplog.text
    assert "database=odoo_db" in caplog.text
    assert "username=reader" in caplog.text
    assert "url-secret" not in caplog.text


def test_retroactive_journal_line_change_is_reported_against_previous_snapshot(tmp_path):
    database = str(tmp_path / "history.sqlite3")
    initialize(database)
    previous = demo_report(date(2026, 1, 1), date(2026, 1, 31), "posted", "company")
    save_run(database, previous)

    current = demo_report(date(2026, 1, 1), date(2026, 1, 31), "posted", "company")
    target_line = next(
        line
        for account in current["accounts"]
        for line in account["target_lines"]
        if line["line_id"] == 5001
    )
    target_line["debit"] = "122.00"
    changes = detect_retroactive_changes(database, current)

    assert len(changes) == 1
    assert changes[0]["type"] == "retroactive_change"
    assert changes[0]["details"]["previous"]["debit"] == "121.00"
    assert changes[0]["details"]["current"]["debit"] == "122.00"


def test_scheduler_claims_are_idempotent_and_failed_runs_can_retry(tmp_path):
    database = str(tmp_path / "schedule.sqlite3")
    initialize(database)
    assert claim_schedule(database, "nightly:demo:2026-01-31")
    assert not claim_schedule(database, "nightly:demo:2026-01-31")
    finish_schedule(database, "nightly:demo:2026-01-31", "temporary failure")
    assert claim_schedule(database, "nightly:demo:2026-01-31")
    finish_schedule(database, "nightly:demo:2026-01-31")
    assert not claim_schedule(database, "nightly:demo:2026-01-31")
