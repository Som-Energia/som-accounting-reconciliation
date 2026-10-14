from datetime import date
from decimal import Decimal

from app.domain import JournalItem, build_movements, decimal_amount


def line(system, account, debit, credit, *, source_model="", source_id=None, native=False, eligible=False):
    return JournalItem(
        system=system,
        line_id=1,
        move_id=10,
        account_code=account,
        account_name=account,
        date=date(2026, 1, 5),
        debit=Decimal(debit),
        credit=Decimal(credit),
        source_model=source_model,
        source_id=source_id,
        is_native=native,
        sync_eligible=eligible,
    )


def test_decimal_parser_uses_decimal_string_and_euro_rounding():
    assert decimal_amount(0.105) == Decimal("0.11")
    assert decimal_amount("0.10") + decimal_amount("0.20") == Decimal("0.30")


def test_many_source_accounts_aggregate_into_one_odoo_account():
    movements, issues = build_movements(
        [line("source", "4301", "10.10", "0"), line("source", "4302", "4.20", "0")],
        [line("target", "430", "14.30", "0")],
        {"4301": ("430", "Customers"), "4302": ("430", "Customers")},
    )
    assert not issues
    assert movements["430"].source_account_codes == {"4301", "4302"}
    assert movements["430"].definitive_debit_difference == Decimal("0.00")


def test_pending_source_operation_is_deferred_from_definitive_difference():
    source = line("source", "4301", "90.00", "90.00", source_model="account.invoice", source_id=7, eligible=True)
    movements, _issues = build_movements(
        [source],
        [],
        {"4301": ("430", "Customers")},
        pending_identities={("account.invoice", 7)},
    )
    row = movements["430"]
    assert row.source.debit == Decimal("90.00")
    assert row.pending_source.credit == Decimal("90.00")
    assert row.definitive_debit_difference == Decimal("0.00")
    assert row.definitive_credit_difference == Decimal("0.00")


def test_synced_scope_excludes_noneligible_source_and_native_target_lines():
    movements, _issues = build_movements(
        [line("source", "4301", "5", "0", eligible=False)],
        [line("target", "430", "5", "0", native=True)],
        {"4301": ("430", "Customers")},
        scope="synced",
    )
    assert movements == {}
