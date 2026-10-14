from datetime import date
from decimal import Decimal

from app.demo import demo_report


def test_demo_covers_invoice_entry_bank_flow_pending_and_many_to_one_mapping():
    report = demo_report(date(2026, 1, 1), date(2026, 1, 31), "posted", "company")
    accounts = {row["account_code"]: row for row in report["accounts"]}

    customers = accounts["430000"]
    assert customers["source_account_codes"] == ["430000001", "430000002"]
    assert customers["source_debit"] == "231.00"
    assert customers["pending_debit"] == "80.00"
    assert customers["target_native_debit"] == "15.00"
    assert customers["definitive_debit_difference"] == "-15.00"

    bank = accounts["572000"]
    assert bank["source_debit"] == "50.00"
    assert bank["target_debit"] == "50.00"

    assert any(issue["type"] == "pending_sync" and issue.get("source_id") == 103 for issue in report["issues"])
    assert any(issue["type"] == "temporal_operation" and issue.get("source_id") == 900 for issue in report["issues"])
    assert any(issue["type"] == "excluded_operation" and 200 in issue["details"]["operation_ids"] for issue in report["issues"])
    assert any(issue["type"] == "unmapped_account" and issue["account_code"] == "410000001" for issue in report["issues"])
    assert not any(issue["type"] == "duplicate_origin" for issue in report["issues"])
    assert report["cumulative_status"] == "informational_opening_unvalidated"


def test_full_company_includes_odoo_native_but_synced_scope_excludes_it():
    full = demo_report(date(2026, 1, 1), date(2026, 1, 31), "posted", "company")
    synced = demo_report(date(2026, 1, 1), date(2026, 1, 31), "posted", "synced")
    full_accounts = {row["account_code"]: row for row in full["accounts"]}
    synced_accounts = {row["account_code"]: row for row in synced["accounts"]}
    assert full_accounts["430000"]["target_native_debit"] == "15.00"
    assert synced_accounts["430000"]["target_native_debit"] == "0.00"
    assert synced_accounts["572000"]["source_debit"] == "50.00"


def test_debit_and_credit_are_checked_even_when_net_balance_is_zero():
    report = demo_report(date(2026, 1, 1), date(2026, 1, 31), "posted", "company")
    row = next(item for item in report["accounts"] if item["account_code"] == "700000")
    assert Decimal(row["definitive_debit_difference"]) == Decimal("0.00")
    assert Decimal(row["definitive_credit_difference"]) != Decimal("0.00")
    assert row["status"] == "movement_difference"
