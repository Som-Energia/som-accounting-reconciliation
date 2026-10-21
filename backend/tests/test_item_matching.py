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
    description="",
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
        "description": description,
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


def test_invoice_lines_match_grouped_target_by_invoice_reference_and_account():
    source = [
        line(
            1,
            system="source",
            debit="0.00",
            credit="8.37",
            description="29253556 AML 342088520 · FE2601165896",
        ),
        line(
            2,
            system="source",
            debit="0.00",
            credit="1.12",
            description="29253556 AML 342088519 · FE2601165896",
        ),
    ]
    target = [
        line(
            11,
            system="target",
            model="account.move",
            source_id=None,
            debit="0.00",
            credit="9.49",
            description="FE2601165896 AML 13385595 · Agrupació 700000000101",
        )
    ]

    source_result, target_result, matched_count = match_journal_items(source, target)

    assert matched_count == 1
    assert all(item["matched"] for item in source_result)
    assert target_result[0]["matched"]


def test_grouped_invoice_match_supports_receipt_and_year_slash_references():
    for reference in ("RE2601165896", "2026/12345"):
        source = [
            line(
                1,
                system="source",
                debit="0.00",
                credit="4.00",
                description=f"Invoice {reference}",
            ),
            line(
                2,
                system="source",
                debit="0.00",
                credit="5.00",
                description=f"Invoice {reference}",
            ),
        ]
        target = [
            line(
                11,
                system="target",
                model="",
                source_id=None,
                debit="0.00",
                credit="9.00",
                description=f"Grouped {reference}",
            )
        ]

        source_result, target_result, matched_count = match_journal_items(source, target)

        assert matched_count == 1
        assert all(item["matched"] for item in source_result)
        assert target_result[0]["matched"]


def test_invoice_lines_from_different_invoices_or_accounts_do_not_group_match():
    source = [
        line(1, system="source", debit="0.00", credit="8.37", description="FE2601165896"),
        line(2, system="source", debit="0.00", credit="1.12", description="RE2601165896"),
    ]
    target = [
        line(
            11,
            system="target",
            model="",
            source_id=None,
            debit="0.00",
            credit="9.49",
            description="FE2601165896",
        )
    ]

    source_result, target_result, matched_count = match_journal_items(source, target)

    assert matched_count == 0
    assert not any(item["matched"] for item in source_result)
    assert not target_result[0]["matched"]

    source = [
        line(
            1,
            system="source",
            debit="0.00",
            credit="8.37",
            description="FE2601165896",
        ),
        line(
            2,
            system="source",
            account="700001",
            debit="0.00",
            credit="1.12",
            description="FE2601165896",
        ),
    ]

    source_result, target_result, matched_count = match_journal_items(source, target)

    assert matched_count == 0
    assert not any(item["matched"] for item in source_result)
    assert not target_result[0]["matched"]


def test_invoice_group_with_different_totals_does_not_match():
    source = [
        line(
            1,
            system="source",
            debit="0.00",
            credit="8.37",
            description="FE2601165896",
        ),
        line(
            2,
            system="source",
            debit="0.00",
            credit="1.12",
            description="FE2601165896",
        ),
    ]
    target = [
        line(
            11,
            system="target",
            model="",
            source_id=None,
            debit="0.00",
            credit="9.48",
            description="FE2601165896",
        )
    ]

    source_result, target_result, matched_count = match_journal_items(source, target)

    assert matched_count == 0
    assert not any(item["matched"] for item in source_result)
    assert not target_result[0]["matched"]


def test_non_invoice_entries_do_not_use_invoice_reference_grouping():
    source = [
        line(
            1,
            system="source",
            model="account.move",
            debit="0.00",
            credit="4.00",
            description="Entry FE2601165896",
        ),
        line(
            2,
            system="source",
            model="account.move",
            debit="0.00",
            credit="5.00",
            description="Entry FE2601165896",
        ),
    ]
    target = [
        line(
            11,
            system="target",
            model="",
            source_id=None,
            debit="0.00",
            credit="9.00",
            description="Entry FE2601165896",
        )
    ]

    source_result, target_result, matched_count = match_journal_items(source, target)

    assert matched_count == 0
    assert not any(item["matched"] for item in source_result)
    assert not target_result[0]["matched"]


def test_non_invoice_entries_still_match_by_exact_erp_origin():
    source = [
        line(
            1,
            system="source",
            model="account.move",
            source_id=42,
            debit="15.00",
            credit="0.00",
        )
    ]
    target = [
        line(
            11,
            system="target",
            model="account.move",
            source_id=42,
            debit="15.00",
            credit="0.00",
        )
    ]

    source_result, target_result, matched_count = match_journal_items(source, target)

    assert matched_count == 1
    assert source_result[0]["matched"]
    assert target_result[0]["matched"]
