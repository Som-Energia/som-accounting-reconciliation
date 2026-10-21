from __future__ import annotations

import re
from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any


_INVOICE_REFERENCE = re.compile(
    r"(?<![A-Z0-9])(?:[A-Z]{2,}[A-Z0-9-]*\d[A-Z0-9-]*|"
    r"(?:19|20)\d{2}/\d+)(?![A-Z0-9])",
    re.IGNORECASE,
)


def _match_key(item: Mapping[str, Any]) -> tuple[Any, ...] | None:
    source_model = item.get("source_model")
    source_id = item.get("source_id")
    if item.get("is_native") or not source_model or source_id is None:
        return None
    return (
        source_model,
        source_id,
        item.get("mapped_account_code") or item.get("account_code"),
        item.get("date"),
        item.get("debit"),
        item.get("credit"),
    )


def _invoice_reference(item: Mapping[str, Any]) -> str | None:
    if item.get("is_native"):
        return None
    references = {
        match.group(0).upper()
        for match in _INVOICE_REFERENCE.finditer(str(item.get("description") or ""))
        if not match.group(0).upper().startswith("AML")
    }
    return next(iter(references)) if len(references) == 1 else None


def _invoice_match_key(item: Mapping[str, Any]) -> tuple[Any, ...] | None:
    source_model = item.get("source_model")
    if item.get("system") == "source":
        if source_model != "account.invoice":
            return None
    elif source_model and source_model not in {"account.invoice", "account.move"}:
        return None
    reference = _invoice_reference(item)
    if reference is None:
        return None
    return (
        reference,
        item.get("mapped_account_code") or item.get("account_code"),
        item.get("date"),
    )


def _amount(item: Mapping[str, Any], field: str) -> Decimal:
    return Decimal(str(item.get(field) or "0"))


def match_journal_items(
    source_items: Sequence[Mapping[str, Any]],
    target_items: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Mark exact pairs and invoice-scoped groups without collapsing line details."""
    source = [dict(item, matched=False) for item in source_items]
    target = [dict(item, matched=False) for item in target_items]

    target_positions: dict[tuple[Any, ...], deque[int]] = defaultdict(deque)
    for index, item in enumerate(target):
        key = _match_key(item)
        if key is not None:
            target_positions[key].append(index)

    matched_count = 0
    for item in source:
        key = _match_key(item)
        if key is None or not target_positions[key]:
            continue
        target_index = target_positions[key].popleft()
        item["matched"] = True
        target[target_index]["matched"] = True
        matched_count += 1

    source_groups: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    target_groups: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    for index, item in enumerate(source):
        if not item["matched"]:
            key = _invoice_match_key(item)
            if key is not None:
                source_groups[key].append(index)
    for index, item in enumerate(target):
        if not item["matched"]:
            key = _invoice_match_key(item)
            if key is not None:
                target_groups[key].append(index)

    for key, source_indexes in source_groups.items():
        target_indexes = target_groups.get(key)
        if not target_indexes:
            continue
        source_total = (
            sum((_amount(source[index], "debit") for index in source_indexes), Decimal("0")),
            sum((_amount(source[index], "credit") for index in source_indexes), Decimal("0")),
        )
        target_total = (
            sum((_amount(target[index], "debit") for index in target_indexes), Decimal("0")),
            sum((_amount(target[index], "credit") for index in target_indexes), Decimal("0")),
        )
        if source_total != target_total:
            continue
        for index in source_indexes:
            source[index]["matched"] = True
        for index in target_indexes:
            target[index]["matched"] = True
        matched_count += 1

    return source, target, matched_count
