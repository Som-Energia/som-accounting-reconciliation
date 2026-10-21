from app.item_matching import match_journal_items


def line(
    line_id,
    *,
    system,
    model="account.invoice",
    source_id=7,
    account="430000",
    date="2026-01-15",
    debit="100.00",
    credit="0.00",
    native=False,
):
    return {
        "line_id": line_id,
        "system": system,
        "source_model": model,
        "source_id": source_id,
        "account_code": account,
        "mapped_account_code": account,
        "date": date,
        "debit": debit,
        "credit": credit,
        "is_native": native,
    }


def test_journal_item_match_requires_same_origin_account_date_and_amounts():
    source = [
        line(1, system="source"),
        line(2, system="source", date="2026-01-16"),
        line(3, system="source", credit="1.00"),
        line(4, system="source", account="700000"),
        line(5, system="source", source_id=8),
        line(6, system="source", model="", source_id=None),
    ]
    target = [
        line(11, system="target"),
        line(12, system="target", date="2026-01-15"),
        line(13, system="target", credit="2.00"),
        line(14, system="target", account="430000"),
        line(15, system="target", source_id=9),
        line(16, system="target", model="", source_id=None, native=True),
    ]

    source_result, target_result, matched_count = match_journal_items(source, target)

    assert matched_count == 1
    assert [item["matched"] for item in source_result] == [True, False, False, False, False, False]
    assert [item["matched"] for item in target_result] == [True, False, False, False, False, False]


def test_duplicate_origins_match_one_to_one():
    source = [line(1, system="source"), line(2, system="source")]
    target = [line(11, system="target")]

    source_result, target_result, matched_count = match_journal_items(source, target)

    assert matched_count == 1
    assert [item["matched"] for item in source_result] == [True, False]
    assert [item["matched"] for item in target_result] == [True]


def test_source_account_uses_its_mapped_target_account_for_matching():
    source = [line(1, system="source", account="430000000001")]
    source[0]["mapped_account_code"] = "430000"
    target = [line(11, system="target", account="430000")]

    source_result, target_result, matched_count = match_journal_items(source, target)

    assert matched_count == 1
    assert source_result[0]["matched"]
    assert target_result[0]["matched"]


def test_native_odoo_movements_are_never_hidden_as_matches():
    source = [line(1, system="source")]
    target = [line(11, system="target", native=True)]

    source_result, target_result, matched_count = match_journal_items(source, target)

    assert matched_count == 0
    assert not source_result[0]["matched"]
    assert not target_result[0]["matched"]
