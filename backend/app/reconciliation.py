from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .connectors import ERPProfile, make_reader
from .domain import JournalItem, amount_text, build_movements


def _serialize_line(line: JournalItem) -> dict[str, Any]:
    return {
        "system": line.system,
        "line_id": line.line_id,
        "move_id": line.move_id,
        "account_code": line.account_code,
        "mapped_account_code": line.mapped_account_code or line.account_code,
        "account_name": line.account_name,
        "date": line.date.isoformat(),
        "debit": amount_text(line.debit),
        "credit": amount_text(line.credit),
        "journal": line.journal,
        "move_label": line.move_label,
        "description": line.description,
        "source_model": line.source_model,
        "source_id": line.source_id,
        "is_native": line.is_native,
        "is_reversal": line.is_reversal,
    }


def _next_month_close(period_end: date) -> date:
    if period_end.month == 12:
        next_month = date(period_end.year + 1, 1, 1)
    else:
        next_month = date(period_end.year, period_end.month + 1, 1)
    following_month = date(
        next_month.year + (next_month.month == 12),
        1 if next_month.month == 12 else next_month.month + 1,
        1,
    )
    return following_month - timedelta(days=1)


def _origin_index(lines: list[JournalItem]) -> dict[tuple[str, int], set[int]]:
    index: dict[tuple[str, int], set[int]] = defaultdict(set)
    for line in lines:
        if line.source_model and line.source_id is not None:
            index[(line.source_model, line.source_id)].add(line.move_id)
    return index


def _build_aggregate_report(
    profile: dict[str, Any],
    date_from: date,
    date_to: date,
    state_filter: str,
    scope: str,
    source: Any,
    target: Any,
    source_accounts: list[dict[str, Any]],
    target_accounts: list[dict[str, Any]],
    account_mapping: dict[str, tuple[str, str]],
    issues: list[dict[str, Any]],
    comparison_date_to: date | None = None,
) -> dict[str, Any]:
    """Use ERP-side account aggregates and load AML details only on drill-down."""
    source_totals = source.period_totals(
        int(profile["source"]["company_id"]), date_from, date_to, state_filter
    )
    target_totals, target_synced = target.period_totals(
        int(profile["target"]["company_id"]), date_from, date_to, state_filter
    )
    source_by_target: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: {"debit": Decimal("0.00"), "credit": Decimal("0.00")}
    )
    source_codes_by_target: dict[str, set[str]] = defaultdict(set)
    for source_code, (debit, credit) in source_totals.items():
        mapped = account_mapping.get(source_code)
        if not mapped:
            continue
        target_code, _target_name = mapped
        source_by_target[target_code]["debit"] += debit
        source_by_target[target_code]["credit"] += credit
        source_codes_by_target[target_code].add(source_code)

    target_names = {
        str(account.get("code_store") or account.get("code") or ""): str(account.get("name") or "")
        for account in target_accounts
    }
    codes = sorted(
        set(source_by_target)
        | set(target_totals)
        | {account_mapping[code][0] for code in account_mapping}
    )
    age_cutoff = _next_month_close(date_to)
    today = datetime.now(ZoneInfo("Europe/Madrid")).date()
    rows = []
    for code in codes:
        expected = source_by_target.get(
            code, {"debit": Decimal("0.00"), "credit": Decimal("0.00")}
        )
        actual = target_totals.get(code, (Decimal("0.00"), Decimal("0.00")))
        synced = target_synced.get(code, (Decimal("0.00"), Decimal("0.00")))
        native = (actual[0] - synced[0], actual[1] - synced[1])
        debit_difference = expected["debit"] - actual[0]
        credit_difference = expected["credit"] - actual[1]
        balance_difference = (expected["debit"] - expected["credit"]) - (actual[0] - actual[1])
        status = "balanced"
        if debit_difference or credit_difference:
            status = "movement_difference" if today > age_cutoff else "temporal_difference"
            issues.append(
                {
                    "type": status,
                    "account_code": code,
                    "title": f"Movement difference on Odoo account {code}.",
                    "details": {
                        "date_from": date_from.isoformat(),
                        "date_to": date_to.isoformat(),
                        "ageing_cutoff": age_cutoff.isoformat(),
                        "scope": scope,
                        "debit_difference": amount_text(debit_difference),
                        "credit_difference": amount_text(credit_difference),
                        "balance_difference": amount_text(balance_difference),
                    },
                }
            )
        rows.append(
            {
                "account_code": code,
                "account_name": target_names.get(code, ""),
                "source_account_codes": sorted(source_codes_by_target.get(code, set())),
                "source_debit": amount_text(expected["debit"]),
                "source_credit": amount_text(expected["credit"]),
                "pending_debit": "0.00",
                "pending_credit": "0.00",
                "target_synced_debit": amount_text(synced[0]),
                "target_synced_credit": amount_text(synced[1]),
                "target_native_debit": amount_text(native[0]),
                "target_native_credit": amount_text(native[1]),
                "target_debit": amount_text(actual[0]),
                "target_credit": amount_text(actual[1]),
                "debit_difference": amount_text(debit_difference),
                "credit_difference": amount_text(credit_difference),
                "balance_difference": amount_text(balance_difference),
                "definitive_balance_difference": amount_text(balance_difference),
                "definitive_debit_difference": amount_text(debit_difference),
                "definitive_credit_difference": amount_text(credit_difference),
                "status": status,
                "source_lines": [],
                "target_lines": [],
            }
        )

    history_start = date.fromisoformat(profile.get("history_start", "1900-01-01"))
    cumulative_available = date_from <= history_start
    source_cumulative = source_totals if cumulative_available else None
    target_cumulative = target_totals if cumulative_available else None
    cumulative: dict[str, dict[str, str]] = {}
    if source_cumulative is not None and target_cumulative is not None:
        for source_code, (debit, credit) in source_cumulative.items():
            mapped = account_mapping.get(source_code)
            if not mapped:
                continue
            target_code = mapped[0]
            values = cumulative.setdefault(
                target_code,
                {"source_debit": "0.00", "source_credit": "0.00", "target_debit": "0.00", "target_credit": "0.00"},
            )
            values["source_debit"] = amount_text(Decimal(values["source_debit"]) + debit)
            values["source_credit"] = amount_text(Decimal(values["source_credit"]) + credit)
        for code, (debit, credit) in target_cumulative.items():
            values = cumulative.setdefault(
                code,
                {"source_debit": "0.00", "source_credit": "0.00", "target_debit": "0.00", "target_credit": "0.00"},
            )
            values["target_debit"] = amount_text(debit)
            values["target_credit"] = amount_text(credit)

    historical_comparison: dict[str, dict[str, str]] = {}
    if comparison_date_to:
        if comparison_date_to == date_to:
            historical_comparison = {
                row["account_code"]: {
                    "source_debit": row["source_debit"],
                    "source_credit": row["source_credit"],
                    "target_debit": row["target_debit"],
                    "target_credit": row["target_credit"],
                }
                for row in rows
            }
        elif comparison_date_to < date_to:
            overlap_source = source.period_totals(
                int(profile["source"]["company_id"]), date_from, comparison_date_to, state_filter
            )
            overlap_target, _overlap_synced = target.period_totals(
                int(profile["target"]["company_id"]), date_from, comparison_date_to, state_filter
            )
            overlap_by_target: dict[str, dict[str, Decimal]] = defaultdict(
                lambda: {"debit": Decimal("0.00"), "credit": Decimal("0.00")}
            )
            for source_code, (debit, credit) in overlap_source.items():
                mapping = account_mapping.get(source_code)
                if mapping:
                    overlap_by_target[mapping[0]]["debit"] += debit
                    overlap_by_target[mapping[0]]["credit"] += credit
            for code in sorted(set(overlap_by_target) | set(overlap_target)):
                source_amounts = overlap_by_target.get(
                    code, {"debit": Decimal("0.00"), "credit": Decimal("0.00")}
                )
                target_amounts = overlap_target.get(
                    code, (Decimal("0.00"), Decimal("0.00"))
                )
                historical_comparison[code] = {
                    "source_debit": amount_text(source_amounts["debit"]),
                    "source_credit": amount_text(source_amounts["credit"]),
                    "target_debit": amount_text(target_amounts[0]),
                    "target_credit": amount_text(target_amounts[1]),
                }

    sync_operations = source.sync_operations(
        int(profile["source"]["company_id"]), date_from, date_to, state_filter
    )
    payment_operations_method = getattr(source, "payment_operations", None)
    payment_operations = payment_operations_method(int(profile["source"]["company_id"])) if payment_operations_method else []
    payment_operations = [
        operation for operation in payment_operations
        if not operation.get("date") or date_from.isoformat() <= operation["date"] <= date_to.isoformat()
    ]
    operations = sync_operations + payment_operations
    candidates = {
        (str(operation["source_model"]), int(operation["source_id"]))
        for operation in operations
        if operation.get("source_model") and operation.get("source_id") is not None
    }
    origins = target.origins(int(profile["target"]["company_id"]), candidates)
    operation_groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for operation in operations:
        model = str(operation["source_model"])
        source_id = int(operation["source_id"])
        if operation.get("sync_classification") == "excluded":
            reason = str(operation.get("reason") or "No enabled synchronization route applies.")
            group_key = str(operation.get("journal") or "")
            operation_groups[("excluded_operation", model, group_key, reason)].append(operation)
            continue
        matches = [
            item for item in origins.get((model, source_id), [])
            if item.get("posted", True)
        ]
        if not matches:
            group_key = str(operation.get("journal") or operation.get("payment_mode") or "")
            operation_groups[("pending_sync", model, group_key, "")].append(operation)
        elif operation.get("date") and not any(
            item.get("date") == operation["date"] for item in matches
        ):
            group_key = str(operation.get("journal") or operation.get("payment_mode") or "")
            operation_groups[("temporal_operation", model, group_key, "")].append(
                {**operation, "target_records": matches}
            )
    for (issue_type, model, group_key, reason), grouped_operations in operation_groups.items():
        label = group_key or "unspecified journal/payment mode"
        if issue_type == "excluded_operation":
            title = f"{len(grouped_operations)} {model} operation(s) excluded in {label}: {reason}"
        elif issue_type == "pending_sync":
            title = f"{len(grouped_operations)} {model} operation(s) have no posted Odoo origin in {label}."
        else:
            title = f"{len(grouped_operations)} {model} operation(s) have different accounting dates in {label}."
        issues.append({
            "type": issue_type,
            "source_model": model,
            "title": title,
            "details": {
                "group_key": group_key,
                "journal_or_payment_mode": label,
                "reason": reason,
                "operation_count": len(grouped_operations),
                "sample_operations": grouped_operations[:100],
            },
        })

    mapping_issue_types = {"unmapped_account", "missing_target_account", "ambiguous_mapping"}
    return {
        "company": {"key": profile["key"], "name": profile["name"]},
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "state_filter": state_filter,
        "scope": scope,
        "currency": "EUR",
        "comparison_basis": "period_movement",
        "details_mode": "live",
        "aggregate_schema": "aggregate-v1",
        "historical_comparison": historical_comparison,
        "pending_adjustment_available": False,
        "cumulative_scope": "company",
        "cumulative_status": "informational_opening_unvalidated",
        "cumulative_data_available": source_cumulative is not None and target_cumulative is not None,
        "cumulative_warning": "Company cumulative balances are informational until opening entries are loaded and validated in Odoo; they are shown independently of the selected movement perimeter.",
        "scope_warning": "Pending operations are listed separately; their AML amounts are not allocated into the account totals in aggregate mode. Differences remain temporal until the close threshold." if operations else "",
        "cumulative_available": cumulative,
        "accounts": rows,
        "issues": issues,
        "totals": {
            "account_count": len(rows),
            "issue_count": len(issues),
            "mapping_complete": not any(issue["type"] in mapping_issue_types for issue in issues),
            "mapping_issue_count": sum(issue["type"] in mapping_issue_types for issue in issues),
            "source_debit": amount_text(sum((Decimal(row["source_debit"]) for row in rows), Decimal("0"))),
            "source_credit": amount_text(sum((Decimal(row["source_credit"]) for row in rows), Decimal("0"))),
            "target_debit": amount_text(sum((Decimal(row["target_debit"]) for row in rows), Decimal("0"))),
            "target_credit": amount_text(sum((Decimal(row["target_credit"]) for row in rows), Decimal("0"))),
        },
    }


def _build_aggregate_report_legacy(
    profile: dict[str, Any],
    date_from: date,
    date_to: date,
    state_filter: str,
    scope: str,
    source: Any,
    target: Any,
    source_accounts: list[dict[str, Any]],
    target_accounts: list[dict[str, Any]],
    account_mapping: dict[str, tuple[str, str]],
    issues: list[dict[str, Any]],
    comparison_date_to: date | None = None,
) -> dict[str, Any]:
    """Deprecated aggregate implementation retained during the query migration."""
    source_totals = source.period_totals(
        int(profile["source"]["company_id"]), date_from, date_to, state_filter
    )
    target_totals, target_synced = target.period_totals(
        int(profile["target"]["company_id"]), date_from, date_to, state_filter
    )
    source_by_target: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: {"debit": Decimal("0.00"), "credit": Decimal("0.00")}
    )
    source_codes_by_target: dict[str, set[str]] = defaultdict(set)
    for source_code, (debit, credit) in source_totals.items():
        mapped = account_mapping.get(source_code)
        if not mapped:
            continue
        target_code, _target_name = mapped
        source_by_target[target_code]["debit"] += debit
        source_by_target[target_code]["credit"] += credit
        source_codes_by_target[target_code].add(source_code)

    target_names = {
        str(account.get("code_store") or account.get("code") or ""): str(account.get("name") or "")
        for account in target_accounts
    }
    codes = sorted(set(source_by_target) | set(target_totals) | set(account_mapping[code][0] for code in account_mapping))
    aging_cutoff = _next_month_close(date_to)
    today = datetime.now(ZoneInfo("Europe/Madrid")).date()
    rows = []
    for code in codes:
        source_values = source_by_target.get(code, {"debit": Decimal("0.00"), "credit": Decimal("0.00")})
        actual = target_totals.get(code, (Decimal("0.00"), Decimal("0.00")))
        synced = target_synced.get(code, (Decimal("0.00"), Decimal("0.00")))
        native = (actual[0] - synced[0], actual[1] - synced[1])
        debit_difference = source_values["debit"] - actual[0]
        credit_difference = source_values["credit"] - actual[1]
        balance_difference = (source_values["debit"] - source_values["credit"]) - (actual[0] - actual[1])
        issue_type = None
        if debit_difference or credit_difference:
            issue_type = "movement_difference" if today > aging_cutoff else "temporal_difference"
            issues.append({
                "type": issue_type,
                "account_code": code,
                "title": f"Movement difference on Odoo account {code}.",
                "details": {
                    "date_from": date_from.isoformat(),
                    "date_to": date_to.isoformat(),
                    "ageing_cutoff": aging_cutoff.isoformat(),
                    "scope": scope,
                    "debit_difference": amount_text(debit_difference),
                    "credit_difference": amount_text(credit_difference),
                    "balance_difference": amount_text(balance_difference),
                },
            })
        rows.append({
            "account_code": code,
            "account_name": target_names.get(code, ""),
            "source_account_codes": sorted(source_codes_by_target.get(code, set())),
            "source_debit": amount_text(source_values["debit"]),
            "source_credit": amount_text(source_values["credit"]),
            "pending_debit": "0.00",
            "pending_credit": "0.00",
            "target_synced_debit": amount_text(synced[0]),
            "target_synced_credit": amount_text(synced[1]),
            "target_native_debit": amount_text(native[0]),
            "target_native_credit": amount_text(native[1]),
            "target_debit": amount_text(actual[0]),
            "target_credit": amount_text(actual[1]),
            "debit_difference": amount_text(debit_difference),
            "credit_difference": amount_text(credit_difference),
            "balance_difference": amount_text(balance_difference),
            "definitive_balance_difference": amount_text(balance_difference),
            "definitive_debit_difference": amount_text(debit_difference),
            "definitive_credit_difference": amount_text(credit_difference),
            "status": issue_type or "balanced",
            "source_lines": [],
            "target_lines": [],
        })

    history_start = date.fromisoformat(profile.get("history_start", "1900-01-01"))
    cumulative_available = date_from <= history_start
    source_cumulative = source_totals if cumulative_available else None
    target_cumulative = target_totals if cumulative_available else None
    cumulative: dict[str, dict[str, str]] = {}
    if source_cumulative is not None and target_cumulative is not None:
        for source_code, (debit, credit) in source_cumulative.items():
            mapped = account_mapping.get(source_code)
            if not mapped:
                continue
            target_code, _name = mapped
            row = cumulative.setdefault(target_code, {
                "source_debit": "0.00", "source_credit": "0.00",
                "target_debit": "0.00", "target_credit": "0.00",
            })
            row["source_debit"] = amount_text(Decimal(row["source_debit"]) + debit)
            row["source_credit"] = amount_text(Decimal(row["source_credit"]) + credit)
        for code, (debit, credit) in target_cumulative.items():
            row = cumulative.setdefault(code, {
                "source_debit": "0.00", "source_credit": "0.00",
                "target_debit": "0.00", "target_credit": "0.00",
            })
            row["target_debit"] = amount_text(debit)
            row["target_credit"] = amount_text(credit)

    historical_comparison: dict[str, dict[str, str]] = {}
    if comparison_date_to:
        if comparison_date_to == date_to:
            historical_comparison = {
                row["account_code"]: {
                    "source_debit": row["source_debit"],
                    "source_credit": row["source_credit"],
                    "target_debit": row["target_debit"],
                    "target_credit": row["target_credit"],
                }
                for row in rows
            }
        elif comparison_date_to < date_to:
            overlap_source = source.period_totals(
                int(profile["source"]["company_id"]), date_from, comparison_date_to, state_filter
            )
            overlap_target, _overlap_synced = target.period_totals(
                int(profile["target"]["company_id"]), date_from, comparison_date_to, state_filter
            )
            overlap_by_target: dict[str, dict[str, Decimal]] = defaultdict(
                lambda: {"debit": Decimal("0.00"), "credit": Decimal("0.00")}
            )
            for source_code, (debit, credit) in overlap_source.items():
                mapping = account_mapping.get(source_code)
                if mapping:
                    overlap_by_target[mapping[0]]["debit"] += debit
                    overlap_by_target[mapping[0]]["credit"] += credit
            for code in sorted(set(overlap_by_target) | set(overlap_target)):
                source_amounts = overlap_by_target.get(
                    code, {"debit": Decimal("0.00"), "credit": Decimal("0.00")}
                )
                target_amounts = overlap_target.get(
                    code, (Decimal("0.00"), Decimal("0.00"))
                )
                historical_comparison[code] = {
                    "source_debit": amount_text(source_amounts["debit"]),
                    "source_credit": amount_text(source_amounts["credit"]),
                    "target_debit": amount_text(target_amounts[0]),
                    "target_credit": amount_text(target_amounts[1]),
                }

    sync_operations = source.sync_operations(
        int(profile["source"]["company_id"]), date_from, date_to, state_filter
    )
    payment_operations = source.payment_operations(int(profile["source"]["company_id"]))
    operations = [
        *sync_operations,
        *[
            operation for operation in payment_operations
            if not operation.get("date") or date_from.isoformat() <= operation["date"] <= date_to.isoformat()
        ],
    ]
    candidates = {
        (str(operation["source_model"]), int(operation["source_id"]))
        for operation in operations
        if operation.get("source_model") and operation.get("source_id") is not None
    }
    origins = target.origins(int(profile["target"]["company_id"]), candidates)
    operation_groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for operation in operations:
        model = str(operation["source_model"])
        source_id = int(operation["source_id"])
        classification = operation.get("sync_classification", "payment")
        if classification == "excluded":
            reason = str(operation.get("reason") or "No enabled synchronization route applies.")
            operation_groups[("excluded_operation", model, str(operation.get("journal") or ""), reason)].append(operation)
            continue
        matches = [item for item in origins.get((model, source_id), []) if item.get("posted", True)]
        if not matches:
            operation_groups[("pending_sync", model, str(operation.get("journal") or operation.get("payment_mode") or ""), "")].append(operation)
        elif operation.get("date") and not any(item.get("date") == operation["date"] for item in matches):
            operation_groups[("temporal_operation", model, str(operation.get("journal") or operation.get("payment_mode") or ""), "")].append({**operation, "target_records": matches})
    for (issue_type, model, journal_or_mode, reason), grouped_operations in operation_groups.items():
        label = journal_or_mode or "unspecified journal/payment mode"
        if issue_type == "excluded_operation":
            title = f"{len(grouped_operations)} {model} operation(s) excluded in {label}: {reason}"
        elif issue_type == "pending_sync":
            title = f"{len(grouped_operations)} {model} operation(s) have no posted Odoo origin in {label}."
        else:
            title = f"{len(grouped_operations)} {model} operation(s) have different accounting dates in {label}."
        issues.append({
            "type": issue_type,
            "source_model": model,
            "title": title,
            "details": {
                "group_key": journal_or_mode,
                "journal_or_payment_mode": label,
                "reason": reason,
                "operation_count": len(grouped_operations),
                "operations": grouped_operations,
            },
        })

    mapping_issue_types = {"unmapped_account", "missing_target_account", "ambiguous_mapping"}
    return {
        "company": {"key": profile["key"], "name": profile["name"]},
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "state_filter": state_filter,
        "scope": scope,
        "currency": "EUR",
        "comparison_basis": "period_movement",
        "details_mode": "live",
        "aggregate_schema": "aggregate-v1",
        "historical_comparison": historical_comparison,
        "pending_adjustment_available": False,
        "cumulative_scope": "company",
        "cumulative_status": "informational_opening_unvalidated",
        "cumulative_data_available": source_cumulative is not None and target_cumulative is not None,
        "cumulative_warning": "Company cumulative balances are informational until opening entries are loaded and validated in Odoo; they are shown independently of the selected movement perimeter.",
        "scope_warning": "Some period differences cannot be allocated to a single pending operation without line-level source identity." if scope == "synced" else "",
        "cumulative_available": cumulative,
        "accounts": rows,
        "issues": issues,
        "totals": {
            "account_count": len(rows),
            "issue_count": len(issues),
            "mapping_complete": not any(issue["type"] in mapping_issue_types for issue in issues),
            "mapping_issue_count": sum(issue["type"] in mapping_issue_types for issue in issues),
            "source_debit": amount_text(sum((Decimal(row["source_debit"]) for row in rows), Decimal("0"))),
            "source_credit": amount_text(sum((Decimal(row["source_credit"]) for row in rows), Decimal("0"))),
            "target_debit": amount_text(sum((Decimal(row["target_debit"]) for row in rows), Decimal("0"))),
            "target_credit": amount_text(sum((Decimal(row["target_credit"]) for row in rows), Decimal("0"))),
        },
    }


def build_report(
    profile: dict[str, Any],
    date_from: date,
    date_to: date,
    state_filter: str,
    scope: str,
    *,
    reader_factory: Callable[[dict[str, Any]], Any] = make_reader,
    comparison_date_to: date | None = None,
) -> dict[str, Any]:
    if date_from > date_to:
        raise ValueError("Start date must not be after end date.")
    if state_filter not in {"posted", "all"}:
        raise ValueError("State filter must be posted or all.")
    if scope not in {"company", "synced"}:
        raise ValueError("Scope must be company or synced.")

    source = reader_factory(profile["source"])
    target = reader_factory(profile["target"])
    source_company_id = int(profile["source"]["company_id"])
    target_company_id = int(profile["target"]["company_id"])
    source_accounts = source.accounts(source_company_id)
    target_accounts = target.accounts(target_company_id)
    target_by_code: dict[str, dict] = {}
    duplicate_target_codes: set[str] = set()
    for account in target_accounts:
        code = str(account.get("code_store") or account.get("code") or "")
        if code in target_by_code:
            duplicate_target_codes.add(code)
        else:
            target_by_code[code] = account

    account_mapping: dict[str, tuple[str, str]] = {}
    configured_source_codes: set[str] = set()
    issues: list[dict[str, Any]] = []
    for account in source_accounts:
        source_code = str(account.get("code") or "")
        target_code = str(account.get("odoo_account_code") or "")
        if not target_code:
            issues.append(
                {
                    "type": "unmapped_account",
                    "account_code": source_code,
                    "title": f"OpenERP account {source_code} has no Odoo account mapping.",
                    "details": {"account_name": account.get("name") or ""},
                }
            )
            continue
        configured_source_codes.add(source_code)
        target_account = target_by_code.get(target_code)
        if not target_account:
            issues.append(
                {
                    "type": "missing_target_account",
                    "account_code": source_code,
                    "title": f"Mapped Odoo account {target_code} was not found.",
                    "details": {"account_name": account.get("name") or ""},
                }
            )
            continue
        if target_code in duplicate_target_codes:
            issues.append(
                {
                    "type": "ambiguous_mapping",
                    "account_code": source_code,
                    "title": f"More than one Odoo account uses code {target_code}.",
                    "details": {},
                }
            )
            continue
        account_mapping[source_code] = (target_code, str(target_account.get("name") or ""))

    if scope == "company" and state_filter == "posted":
        source_period_totals = getattr(source, "period_totals", None)
        target_period_totals = getattr(target, "period_totals", None)
        if source_period_totals and target_period_totals:
            return _build_aggregate_report(
                profile,
                date_from,
                date_to,
                state_filter,
                scope,
                source,
                target,
                source_accounts,
                target_accounts,
                account_mapping,
                issues,
                comparison_date_to,
            )

    source_lines = source.journal_items(
        source_company_id, date_from, date_to, state_filter
    )
    payment_operations_method = getattr(source, "payment_operations", None)
    payment_operations = payment_operations_method(source_company_id) if payment_operations_method else []
    target_lines = target.journal_items(
        target_company_id, date_from, date_to, state_filter
    )
    target_index = _origin_index(target_lines)
    origins_method = getattr(target, "origins", None)
    target_origins = origins_method(target_company_id) if origins_method else {
        key: [{"target_id": move_id, "date": None, "label": ""} for move_id in ids]
        for key, ids in target_index.items()
    }
    if state_filter == "posted":
        target_origins = {
            key: [record for record in records if record.get("posted", True)]
            for key, records in target_origins.items()
        }
        target_origins = {key: records for key, records in target_origins.items() if records}
    source_lines = [
        replace(line, sync_eligible=True)
        if (
            not line.sync_eligible
            and line.source_model == "account.move"
            and line.source_id is not None
            and target_origins.get(("account.move", line.source_id))
        )
        else line
        for line in source_lines
    ]
    source_index = {
        (line.source_model, line.source_id)
        for line in source_lines
        if line.sync_eligible and line.source_model and line.source_id is not None
    }
    for (model, source_id), records in target_origins.items():
        if len(records) > 1:
            issues.append(
                {
                    "type": "duplicate_origin",
                    "source_model": model,
                    "source_id": source_id,
                    "title": f"More than one Odoo object has origin {model} {source_id}.",
                    "details": {"target_records": records},
                }
            )
    pending_identities = {
        (model, source_id)
        for model, source_id in source_index
        if not target_origins.get((model, source_id))
    }
    out_of_period_identities = {
        (model, source_id)
        for model, source_id in source_index
        if target_origins.get((model, source_id))
        and not target_index.get((model, source_id))
    }
    deferred_identities = pending_identities | out_of_period_identities
    movements, domain_issues = build_movements(
        source_lines,
        target_lines,
        account_mapping,
        scope=scope,
        pending_identities=deferred_identities,
    )

    for issue in domain_issues:
        line = issue["line"]
        existing_issue = next((
            item for item in issues
            if item["type"] in {"unmapped_account", "missing_target_account", "ambiguous_mapping"}
            and item.get("account_code") == line.account_code
        ), None)
        if existing_issue:
            existing_issue["details"].setdefault("source_lines", []).append(_serialize_line(line))
        elif line.account_code in configured_source_codes:
            continue
        else:
            issues.append(
                {
                    "type": "unmapped_account",
                    "account_code": line.account_code,
                    "title": f"OpenERP account {line.account_code} has movements but no mapping.",
                    "details": {
                        "account_name": line.account_name,
                        "source_lines": [_serialize_line(line)],
                    },
                }
            )

    classified_operations: dict[tuple[str, str, int], list[JournalItem]] = {}
    for line in source_lines:
        if line.sync_classification not in {"ambiguous", "excluded"}:
            continue
        key = (line.sync_classification, line.source_model or "account.move", line.source_id or line.move_id)
        classified_operations.setdefault(key, []).append(line)
    excluded_groups: dict[tuple[str, str], list[tuple[int, list[JournalItem]]]] = defaultdict(list)
    for (classification, model, source_id), lines in classified_operations.items():
        if classification == "ambiguous":
            issues.append(
                {
                    "type": "ambiguous_rule",
                    "source_model": model,
                    "source_id": source_id,
                    "title": f"Synchronization rules are ambiguous for {model} {source_id}.",
                    "details": {
                        "source_line_ids": [line.line_id for line in lines],
                        "reasons": sorted({line.sync_reason for line in lines if line.sync_reason}),
                    },
                }
            )
            continue
        reason = next((line.sync_reason for line in lines if line.sync_reason), "No enabled synchronization route applies.")
        journal = next((line.journal for line in lines if line.journal), "Unknown journal")
        excluded_groups[(reason, journal)].append((source_id, lines))
    for (reason, journal), operations in excluded_groups.items():
        issues.append(
            {
                "type": "excluded_operation",
                "title": f"{len(operations)} operation(s) excluded from direct synchronization in {journal}: {reason}",
                "details": {
                    "operation_ids": [source_id for source_id, _lines in operations[:100]],
                    "source_line_ids": [line.line_id for _source_id, lines in operations for line in lines[:10]][:100],
                    "source_account_codes": sorted({line.account_code for _source_id, lines in operations for line in lines}),
                    "operation_count": len(operations),
                    "journal": journal,
                    "reason": reason,
                },
            }
        )

    source_identity_models: dict[int, set[str]] = defaultdict(set)
    for model, source_id in source_index:
        source_identity_models[source_id].add(model)
    target_identity_models: dict[int, set[str]] = defaultdict(set)
    for model, source_id in target_index:
        target_identity_models[source_id].add(model)
    for source_id, models in target_identity_models.items():
        if len(models) > 1 or (source_id in source_identity_models and models != source_identity_models[source_id]):
            issues.append(
                {
                    "type": "ambiguous_origin",
                    "source_id": source_id,
                    "title": f"ERP identifier {source_id} appears under incompatible source models.",
                    "details": {"target_models": sorted(models)},
                }
            )

    for model, source_id in sorted(pending_identities):
        pending_lines = [
            line for line in source_lines
            if (line.source_model, line.source_id) == (model, source_id)
        ]
        issues.append(
            {
                "type": "pending_sync",
                "source_model": model,
                "source_id": source_id,
                "title": f"{model} {source_id} has no matching ERP-origin target entry.",
                "details": {
                    "source_line_ids": [line.line_id for line in pending_lines],
                    "source_debit": amount_text(sum((line.debit for line in pending_lines), Decimal("0"))),
                    "source_credit": amount_text(sum((line.credit for line in pending_lines), Decimal("0"))),
                },
            }
        )

    for model, source_id in sorted(out_of_period_identities):
        target_records = target_origins.get((model, source_id), [])
        issues.append(
            {
                "type": "temporal_operation",
                "source_model": model,
                "source_id": source_id,
                "title": f"{model} {source_id} exists in Odoo outside the selected accounting period.",
                "details": {"target_records": target_records},
            }
        )

    reversal_origins = {
        (line.source_model, line.source_id)
        for line in target_lines
        if line.is_reversal and line.source_model and line.source_id is not None
    }
    for model, source_id in sorted(reversal_origins):
        issues.append(
            {
                "type": "reversal",
                "source_model": model,
                "source_id": source_id,
                "title": f"Odoo contains a reversal for origin {model} {source_id}.",
                "details": {
                    "target_lines": [
                        _serialize_line(line)
                        for line in target_lines
                        if line.is_reversal and (line.source_model, line.source_id) == (model, source_id)
                    ]
                },
            }
        )

    for operation in payment_operations:
        operation_date = date.fromisoformat(operation["date"]) if operation.get("date") else None
        if operation_date and not (date_from <= operation_date <= date_to):
            continue
        identity = (operation["source_model"], int(operation["source_id"]))
        targets = target_origins.get(identity, [])
        if not targets:
            issues.append(
                {
                    "type": "pending_sync",
                    "source_model": identity[0],
                    "source_id": identity[1],
                    "title": f"Payment order {identity[1]} is done and eligible but has no Odoo remittance.",
                    "details": {
                        "operation": operation,
                        "accounting_note": "Payment-order status is diagnostic; its AML contribution is reconciled only through ledger movements.",
                    },
                }
            )
        elif operation_date and not any(
            target_record.get("date") == operation_date.isoformat() for target_record in targets
        ):
            issues.append(
                {
                    "type": "temporal_operation",
                    "source_model": identity[0],
                    "source_id": identity[1],
                    "title": f"Payment order {identity[1]} and its Odoo remittance have different dates.",
                    "details": {"operation": operation, "target_records": targets},
                }
            )

    aging_cutoff = _next_month_close(date_to)
    today = datetime.now(ZoneInfo("Europe/Madrid")).date()
    rows = []
    for code, movement in movements.items():
        issue_type = None
        if (
            movement.definitive_debit_difference != Decimal("0")
            or movement.definitive_credit_difference != Decimal("0")
        ):
            issue_type = "movement_difference" if today > aging_cutoff else "temporal_difference"
            issues.append(
                {
                    "type": issue_type,
                    "account_code": code,
                    "title": f"Movement difference on Odoo account {code}.",
                    "details": {
                        "date_from": date_from.isoformat(),
                        "date_to": date_to.isoformat(),
                        "scope": scope,
                        "ageing_cutoff": aging_cutoff.isoformat(),
                        "balance_difference": amount_text(movement.definitive_balance_difference),
                        "debit_difference": amount_text(movement.definitive_debit_difference),
                        "credit_difference": amount_text(movement.definitive_credit_difference),
                    },
                }
            )
        rows.append(
            {
                "account_code": code,
                "account_name": movement.account_name,
                "source_account_codes": sorted(movement.source_account_codes),
                "source_debit": amount_text(movement.source.debit),
                "source_credit": amount_text(movement.source.credit),
                "pending_debit": amount_text(movement.pending_source.debit),
                "pending_credit": amount_text(movement.pending_source.credit),
                "target_synced_debit": amount_text(movement.target_synced.debit),
                "target_synced_credit": amount_text(movement.target_synced.credit),
                "target_native_debit": amount_text(movement.target_native.debit),
                "target_native_credit": amount_text(movement.target_native.credit),
                "target_debit": amount_text(movement.target.debit),
                "target_credit": amount_text(movement.target.credit),
                "debit_difference": amount_text(movement.debit_difference),
                "credit_difference": amount_text(movement.credit_difference),
                "balance_difference": amount_text(movement.balance_difference),
                "definitive_balance_difference": amount_text(movement.definitive_balance_difference),
                "definitive_debit_difference": amount_text(movement.definitive_debit_difference),
                "definitive_credit_difference": amount_text(movement.definitive_credit_difference),
                "status": issue_type or ("pending" if movement.pending_source.debit or movement.pending_source.credit else "balanced"),
                "source_lines": [_serialize_line(line) for line in movement.source_lines],
                "target_lines": [_serialize_line(line) for line in movement.target_lines],
            }
        )

    source_cumulative = source.cumulative_totals(source_company_id, date_to, state_filter)
    target_cumulative = target.cumulative_totals(target_company_id, date_to, state_filter)
    cumulative: dict[str, dict[str, str]] = {}
    if source_cumulative is not None and target_cumulative is not None:
        for source_code, (debit, credit) in source_cumulative.items():
            mapped = account_mapping.get(source_code)
            if not mapped:
                continue
            target_code, _name = mapped
            row = cumulative.setdefault(
                target_code,
                {"source_debit": "0.00", "source_credit": "0.00", "target_debit": "0.00", "target_credit": "0.00"},
            )
            row["source_debit"] = amount_text(Decimal(row["source_debit"]) + debit)
            row["source_credit"] = amount_text(Decimal(row["source_credit"]) + credit)
        for target_code, (debit, credit) in target_cumulative.items():
            row = cumulative.setdefault(
                target_code,
                {"source_debit": "0.00", "source_credit": "0.00", "target_debit": "0.00", "target_credit": "0.00"},
            )
            row["target_debit"] = amount_text(debit)
            row["target_credit"] = amount_text(credit)
    for row in rows:
        balance = cumulative.get(row["account_code"])
        if balance is None:
            row["cumulative_source_debit"] = "0.00"
            row["cumulative_source_credit"] = "0.00"
            row["cumulative_source_balance"] = "0.00"
            row["cumulative_target_debit"] = "0.00"
            row["cumulative_target_credit"] = "0.00"
            row["cumulative_target_balance"] = "0.00"
        else:
            row["cumulative_source_debit"] = balance["source_debit"]
            row["cumulative_source_credit"] = balance["source_credit"]
            row["cumulative_source_balance"] = amount_text(Decimal(balance["source_debit"]) - Decimal(balance["source_credit"]))
            row["cumulative_target_debit"] = balance["target_debit"]
            row["cumulative_target_credit"] = balance["target_credit"]
            row["cumulative_target_balance"] = amount_text(Decimal(balance["target_debit"]) - Decimal(balance["target_credit"]))

    return {
        "company": {"key": profile["key"], "name": profile["name"]},
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "state_filter": state_filter,
        "scope": scope,
        "currency": "EUR",
        "comparison_basis": "period_movement",
        "cumulative_status": "informational_opening_unvalidated",
        "cumulative_scope": "company",
        "cumulative_data_available": source_cumulative is not None and target_cumulative is not None,
        "cumulative_warning": "Company cumulative balances are informational until opening entries are loaded and validated in Odoo; they are shown independently of the selected movement perimeter.",
        "scope_warning": (
            "Payment-order and bank API accounting lines without a shared stable journal-item origin may remain ambiguous in this perimeter."
            if scope == "synced"
            else ""
        ),
        "cumulative_available": cumulative,
        "accounts": rows,
        "issues": issues,
        "totals": {
            "account_count": len(rows),
            "issue_count": len(issues),
            "mapping_complete": not any(
                issue["type"] in {"unmapped_account", "missing_target_account", "ambiguous_mapping"}
                for issue in issues
            ),
            "mapping_issue_count": sum(
                issue["type"] in {"unmapped_account", "missing_target_account", "ambiguous_mapping"}
                for issue in issues
            ),
            "source_debit": amount_text(sum((Decimal(row["source_debit"]) for row in rows), Decimal("0"))),
            "source_credit": amount_text(sum((Decimal(row["source_credit"]) for row in rows), Decimal("0"))),
            "target_debit": amount_text(sum((Decimal(row["target_debit"]) for row in rows), Decimal("0"))),
            "target_credit": amount_text(sum((Decimal(row["target_credit"]) for row in rows), Decimal("0"))),
        },
    }
