from datetime import date

from app import create_app
from app.demo import demo_report
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


def test_invalid_period_is_rejected(tmp_path):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "api.sqlite3"), "DEMO_MODE": True})
    response = app.test_client().post(
        "/api/runs",
        json={"company": "demo", "date_from": "2026-02-01", "date_to": "2026-01-01"},
    )
    assert response.status_code == 400


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
