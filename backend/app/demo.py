from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from .domain import JournalItem
from .reconciliation import build_report


DEMO_PROFILE = {
    "key": "demo",
    "name": "Demo company (fictional data)",
    "source": {"company_id": 1},
    "target": {"company_id": 10},
}


class DemoReader:
    def __init__(self, side: str):
        self.side = side
        self.accounts_data = self._accounts()
        self.lines = self._lines()

    def _accounts(self) -> list[dict[str, Any]]:
        if self.side == "source":
            return [
                {"id": 1, "code": "430000001", "name": "Customers - electricity", "odoo_account_code": "430000"},
                {"id": 2, "code": "430000002", "name": "Customers - services", "odoo_account_code": "430000"},
                {"id": 3, "code": "572000001", "name": "Bank account", "odoo_account_code": "572000"},
                {"id": 4, "code": "700000001", "name": "Energy sales", "odoo_account_code": "700000"},
                {"id": 5, "code": "477000001", "name": "VAT payable", "odoo_account_code": "477000"},
                {"id": 6, "code": "410000001", "name": "Suppliers (unmapped example)", "odoo_account_code": ""},
            ]
        return [
            {"id": 11, "code": "430000", "code_store": "430000", "name": "Customers"},
            {"id": 12, "code": "572000", "code_store": "572000", "name": "Bank account"},
            {"id": 13, "code": "700000", "code_store": "700000", "name": "Energy sales"},
            {"id": 14, "code": "477000", "code_store": "477000", "name": "VAT payable"},
        ]

    def _lines(self) -> list[JournalItem]:
        def item(
            side: str, line_id: int, move_id: int, code: str, name: str, day: int,
            debit: str, credit: str, journal: str, label: str,
            source_model: str = "", source_id: int | None = None,
            native: bool = False, eligible: bool = False,
            sync_classification: str = "", sync_reason: str = "",
        ) -> JournalItem:
            return JournalItem(
                system=side,
                line_id=line_id,
                move_id=move_id,
                account_code=code,
                account_name=name,
                date=date(2026, 1, day),
                debit=Decimal(debit),
                credit=Decimal(credit),
                journal=journal,
                move_label=label,
                description=label,
                source_model=source_model,
                source_id=source_id,
                is_native=native,
                sync_eligible=eligible,
                sync_classification=sync_classification,
                sync_reason=sync_reason,
            )

        if self.side == "source":
            return [
                item("source", 1001, 100, "430000001", "Customers - electricity", 5, "121.00", "0", "Sales", "INV/2026/001", "account.invoice", 100, eligible=True),
                item("source", 1002, 100, "700000001", "Energy sales", 5, "0", "100.00", "Sales", "INV/2026/001", "account.invoice", 100, eligible=True),
                item("source", 1003, 100, "477000001", "VAT payable", 5, "0", "21.00", "Sales", "INV/2026/001", "account.invoice", 100, eligible=True),
                item("source", 1010, 101, "430000002", "Customers - services", 8, "30.00", "0", "Sales", "INV/2026/002", "account.invoice", 101, eligible=True),
                item("source", 1011, 101, "700000001", "Energy sales", 8, "0", "30.00", "Sales", "INV/2026/002", "account.invoice", 101, eligible=True),
                item("source", 2001, 200, "572000001", "Bank account", 12, "50.00", "0", "Bank", "PAY/2026/001", "account.move", 200, sync_classification="excluded", sync_reason="Direct general-entry synchronization excludes 572 accounts."),
                item("source", 2002, 200, "430000001", "Customers - electricity", 12, "0", "50.00", "Bank", "PAY/2026/001", "account.move", 200, sync_classification="excluded", sync_reason="Direct general-entry synchronization excludes 572 accounts."),
                item("source", 3001, 300, "430000001", "Customers - electricity", 20, "80.00", "0", "Sales", "INV/2026/003", "account.invoice", 103, eligible=True),
                item("source", 3002, 300, "700000001", "Energy sales", 20, "0", "80.00", "Sales", "INV/2026/003", "account.invoice", 103, eligible=True),
                item("source", 4001, 400, "410000001", "Suppliers (unmapped example)", 22, "0", "12.34", "Purchase", "BILL/2026/001"),
            ]
        return [
            item("target", 5001, 500, "430000", "Customers", 5, "121.00", "0", "Sales", "INV/2026/001", "account.invoice", 100),
            item("target", 5002, 500, "700000", "Energy sales", 5, "0", "100.00", "Sales", "INV/2026/001", "account.invoice", 100),
            item("target", 5003, 500, "477000", "VAT payable", 5, "0", "21.00", "Sales", "INV/2026/001", "account.invoice", 100),
            item("target", 5010, 501, "430000", "Customers", 8, "30.00", "0", "Sales", "INV/2026/002", "account.invoice", 101),
            item("target", 5011, 501, "700000", "Energy sales", 8, "0", "30.00", "Sales", "INV/2026/002", "account.invoice", 101),
            item("target", 5020, 502, "572000", "Bank account", 13, "50.00", "0", "Bank", "Statement line", "account.move", 200),
            item("target", 5021, 502, "430000", "Customers", 13, "0", "50.00", "Bank", "Statement line", "account.move", 200),
            item("target", 5030, 503, "430000", "Customers", 24, "15.00", "0", "Manual entry", "Native opening preparation", native=True),
            item("target", 5031, 503, "700000", "Energy sales", 24, "0", "15.00", "Manual entry", "Native opening preparation", native=True),
        ]

    def accounts(self, company_id: int) -> list[dict[str, Any]]:
        return self.accounts_data

    def journal_items(self, company_id: int, date_from: date, date_to: date, state: str) -> list[JournalItem]:
        return [line for line in self.lines if date_from <= line.date <= date_to]

    def cumulative_totals(self, company_id: int, date_to: date, state: str):
        totals: dict[str, tuple[Decimal, Decimal]] = {}
        for line in self.lines:
            if line.date > date_to:
                continue
            debit, credit = totals.get(line.account_code, (Decimal("0"), Decimal("0")))
            totals[line.account_code] = (debit + line.debit, credit + line.credit)
        return totals

    def origins(self, company_id: int):
        origins: dict[tuple[str, int], list[dict[str, Any]]] = {}
        seen: set[tuple[str, int, int]] = set()
        for line in self.lines:
            if line.source_model and line.source_id is not None:
                key = (line.source_model, line.source_id, line.move_id)
                if key in seen:
                    continue
                seen.add(key)
                origins.setdefault((line.source_model, line.source_id), []).append(
                    {"target_id": line.move_id, "date": line.date.isoformat(), "label": line.move_label}
                )
        if self.side == "target":
            origins[("payment.order", 900)] = [
                {"target_id": 700, "date": "2026-01-16", "label": "REM/2026/001"}
            ]
        return origins

    def payment_operations(self, company_id: int):
        if self.side != "source":
            return []
        return [{
            "source_model": "payment.order",
            "source_id": 900,
            "label": "PAYMENT/2026/001",
            "date": "2026-01-15",
            "payment_mode": "Bank transfer",
        }]


def demo_report(date_from: date, date_to: date, state_filter: str, scope: str) -> dict:
    readers = {"source": DemoReader("source"), "target": DemoReader("target")}
    return build_report(
        DEMO_PROFILE,
        date_from,
        date_to,
        state_filter,
        scope,
        reader_factory=lambda config: readers["source" if config is DEMO_PROFILE["source"] else "target"],
    )
