from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from typing import Any


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


def match_journal_items(
    source_items: Sequence[Mapping[str, Any]],
    target_items: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Mark exact source/target movement pairs without collapsing duplicates."""
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

    return source, target, matched_count
