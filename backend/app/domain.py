from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable


ZERO = Decimal("0.00")


def decimal_amount(value: object) -> Decimal:
    if value is None or value is False or value == "":
        return ZERO
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"Invalid accounting amount: {value!r}") from error


def amount_text(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


@dataclass(frozen=True)
class JournalItem:
    system: str
    line_id: int
    move_id: int
    account_code: str
    account_name: str
    date: date
    debit: Decimal
    credit: Decimal
    journal: str = ""
    move_label: str = ""
    description: str = ""
    source_model: str = ""
    source_id: int | None = None
    is_native: bool = False
    sync_eligible: bool = False
    is_reversal: bool = False
    sync_classification: str = ""
    sync_reason: str = ""
    mapped_account_code: str = ""
    partner_id: int | None = None


@dataclass
class Totals:
    debit: Decimal = ZERO
    credit: Decimal = ZERO

    @property
    def balance(self) -> Decimal:
        return self.debit - self.credit

    def add(self, line: JournalItem) -> None:
        self.debit += line.debit
        self.credit += line.credit


@dataclass
class AccountMovement:
    account_code: str
    account_name: str
    source_account_codes: set[str] = field(default_factory=set)
    source: Totals = field(default_factory=Totals)
    target_synced: Totals = field(default_factory=Totals)
    target_native: Totals = field(default_factory=Totals)
    pending_source: Totals = field(default_factory=Totals)
    source_lines: list[JournalItem] = field(default_factory=list)
    target_lines: list[JournalItem] = field(default_factory=list)

    @property
    def target(self) -> Totals:
        return Totals(
            self.target_synced.debit + self.target_native.debit,
            self.target_synced.credit + self.target_native.credit,
        )

    @property
    def debit_difference(self) -> Decimal:
        return self.source.debit - self.target.debit

    @property
    def credit_difference(self) -> Decimal:
        return self.source.credit - self.target.credit

    @property
    def balance_difference(self) -> Decimal:
        return self.source.balance - self.target.balance

    @property
    def definitive_balance_difference(self) -> Decimal:
        return self.source.balance - self.pending_source.balance - self.target.balance

    @property
    def definitive_debit_difference(self) -> Decimal:
        return self.source.debit - self.pending_source.debit - self.target.debit

    @property
    def definitive_credit_difference(self) -> Decimal:
        return self.source.credit - self.pending_source.credit - self.target.credit


def build_movements(
    source_lines: Iterable[JournalItem],
    target_lines: Iterable[JournalItem],
    mapping: dict[str, tuple[str, str]],
    *,
    scope: str = "company",
    pending_identities: set[tuple[str, int]] | None = None,
) -> tuple[dict[str, AccountMovement], list[dict]]:
    """Project source account codes to Odoo codes and aggregate exact amounts."""
    movements: dict[str, AccountMovement] = {}
    issues: list[dict] = []
    pending_identities = pending_identities or set()

    for line in source_lines:
        if scope == "synced" and not line.sync_eligible:
            continue
        mapped = mapping.get(line.account_code)
        if not mapped or not mapped[0]:
            issues.append(
                {
                    "type": "unmapped_account",
                    "account_code": line.account_code,
                    "account_name": line.account_name,
                    "line": line,
                }
            )
            continue
        target_code, target_name = mapped
        movement = movements.setdefault(
            target_code, AccountMovement(target_code, target_name)
        )
        movement.source_account_codes.add(line.account_code)
        line = replace(line, mapped_account_code=target_code)
        movement.source.add(line)
        if line.source_model and line.source_id and (line.source_model, line.source_id) in pending_identities:
            movement.pending_source.add(line)
        movement.source_lines.append(line)

    for line in target_lines:
        if scope == "synced" and line.is_native:
            continue
        movement = movements.setdefault(
            line.account_code,
            AccountMovement(line.account_code, line.account_name),
        )
        if line.is_native:
            movement.target_native.add(line)
        else:
            movement.target_synced.add(line)
        movement.target_lines.append(line)

    return dict(sorted(movements.items())), issues


def totals_by_account(lines: Iterable[JournalItem]) -> dict[str, Totals]:
    totals: dict[str, Totals] = defaultdict(Totals)
    for line in lines:
        totals[line.account_code].add(line)
    return dict(totals)
