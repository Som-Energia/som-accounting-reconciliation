from datetime import date
from decimal import Decimal

from app.demo import demo_report
from app.reconciliation import build_report


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


def test_posted_company_report_uses_erp_side_aggregate_methods():
    profile = {
        "key": "aggregate-test",
        "name": "Aggregate test",
        "source": {"company_id": 1},
        "target": {"company_id": 2},
    }

    class Source:
        def accounts(self, _company_id):
            return [{"id": 1, "code": "123456789012", "name": "Source account", "odoo_account_code": "123456789"}]

        def period_totals(self, *_args):
            return {"123456789012": (Decimal("100.00"), Decimal("20.00"))}

        def cumulative_totals(self, *_args):
            return {"123456789012": (Decimal("500.00"), Decimal("100.00"))}

        def sync_operations(self, *_args):
            return []

        def payment_operations(self, *_args):
            return []

        def journal_items(self, *_args, **_kwargs):
            raise AssertionError("Aggregate report must not load all source AML details")

    class Target:
        def accounts(self, _company_id):
            return [{"id": 2, "code": "123456789", "code_store": "123456789", "name": "Target account"}]

        def period_totals(self, *_args):
            return (
                {"123456789": (Decimal("60.00"), Decimal("10.00"))},
                {"123456789": (Decimal("40.00"), Decimal("10.00"))},
            )

        def cumulative_totals(self, *_args):
            return {"123456789": (Decimal("400.00"), Decimal("80.00"))}

        def origins(self, _company_id, _candidates):
            return {}

        def journal_items(self, *_args, **_kwargs):
            raise AssertionError("Aggregate report must not load all target AML details")

    source, target = Source(), Target()
    report = build_report(
        profile,
        date(2026, 10, 14),
        date(2026, 10, 14),
        "posted",
        "company",
        reader_factory=lambda config: source if config is profile["source"] else target,
    )

    row = next(item for item in report["accounts"] if item["account_code"] == "123456789")
    assert row["source_debit"] == "100.00"
    assert row["target_synced_debit"] == "40.00"
    assert row["target_native_debit"] == "20.00"
    assert row["definitive_debit_difference"] == "40.00"
    assert row["status"] == "temporal_difference"
    assert report["details_mode"] == "live"
