from __future__ import annotations

import os
from pathlib import Path
import xmlrpc.client
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

from .domain import JournalItem, decimal_amount


PAGE_SIZE = 500
ERP_ACCOUNT_BATCH_SIZE = 20
RPC_TIMEOUT_SECONDS = 45


class _BoundedTransport(xmlrpc.client.Transport):
    def make_connection(self, host: str):
        connection = super().make_connection(host)
        connection.timeout = RPC_TIMEOUT_SECONDS
        return connection


class _BoundedSafeTransport(xmlrpc.client.SafeTransport):
    def make_connection(self, host: str):
        connection = super().make_connection(host)
        connection.timeout = RPC_TIMEOUT_SECONDS
        return connection


class ReadOnlyERP(Protocol):
    def accounts(self, company_id: int) -> list[dict[str, Any]]: ...

    def period_totals(
        self, company_id: int, date_from: date, date_to: date, state: str
    ) -> Any: ...

    def journal_items(
        self, company_id: int, date_from: date, date_to: date, state: str
    ) -> list[JournalItem]: ...


@dataclass(frozen=True)
class ERPProfile:
    key: str
    name: str
    source: dict[str, Any]
    target: dict[str, Any]


def _read_secret(config: dict[str, Any]) -> str:
    password_name = config.get("password_env", "")
    password_file_env = config.get("password_file_env", "")
    password_file = os.environ.get(password_file_env) if password_file_env else None
    if password_file:
        try:
            password = Path(password_file).read_text(encoding="utf-8").strip()
        except OSError as error:
            raise ValueError("ERP credential secret file cannot be read.") from error
    else:
        password = os.environ.get(password_name) if password_name else None
    if not password:
        credential_name = password_file_env or password_name
        raise ValueError(f"ERP credential environment variable '{credential_name}' is not set.")
    return password


class ErpPeekReaderClient:
    """Read-only adapter using the repository-standard erppeek Client."""

    def __init__(self, config: dict[str, Any], *, client_factory=None):
        password = _read_secret(config)
        if not config.get("url") or not config.get("database") or not config.get("username"):
            raise ValueError("ERP URL, database, and username must be configured.")
        if client_factory is None:
            try:
                from erppeek import Client
            except ImportError as error:
                raise ValueError("The erppeek package is required for the OpenERP connector.") from error
            client_factory = Client
        transport = (
            _BoundedSafeTransport()
            if config["url"].startswith("https:")
            else _BoundedTransport()
        )
        self.client = client_factory(
            config["url"],
            config["database"],
            config["username"],
            password,
            transport=transport,
        )

    def fields(self, model: str) -> set[str]:
        return set(self.client.fields(model))

    def search(self, model: str, domain: list, *, offset: int = 0) -> list[int]:
        return self.client.search(
            model,
            domain,
            offset=offset,
            limit=PAGE_SIZE,
            order="id asc",
        ) or []

    def read(
        self,
        model: str,
        ids: list[int],
        field_names: list[str],
        *,
        context: dict[str, Any] | None = None,
    ) -> list[dict]:
        if not ids:
            return []
        rows = []
        for offset in range(0, len(ids), PAGE_SIZE):
            page = self.client.read(
                model,
                ids[offset:offset + PAGE_SIZE],
                field_names,
                context=context,
            )
            rows.extend(page if isinstance(page, list) else [page])
        return rows

    def search_read_pages(
        self, model: str, domain: list, field_names: list[str]
    ) -> Iterator[dict]:
        offset = 0
        while True:
            ids = self.search(model, domain, offset=offset)
            if not ids:
                return
            yield from self.read(model, ids, field_names)
            offset += len(ids)
            if len(ids) < PAGE_SIZE:
                return

    def read_group(
        self, model: str, domain: list, fields: list[str], groupby: list[str]
    ) -> list[dict[str, Any]] | None:
        try:
            result = self.client.execute(model, "read_group", domain, fields, groupby)
        except Exception:
            return None
        return result if isinstance(result, list) else None


class OdooXMLRPCReader:
    """Read-only Odoo 18 adapter, using its external XML-RPC API."""

    def __init__(self, config: dict[str, Any], *, proxy_factory=xmlrpc.client.ServerProxy):
        self.config = config
        if not config.get("url") or not config.get("database") or not config.get("username"):
            raise ValueError("ERP URL, database, and username must be configured.")
        password = _read_secret(config)
        if config.get("protocol") != "odoo18":
            raise ValueError("Odoo XML-RPC connector requires protocol 'odoo18'.")
        common_path, object_path = "/xmlrpc/2/common", "/xmlrpc/2/object"
        base_url = config["url"].rstrip("/")
        self.database = config["database"]
        self.password = password
        self.protocol = "odoo18"
        self.proxy_factory = proxy_factory
        self.uid = self._login(self._server(f"{base_url}{common_path}"))
        self.object_proxy = self._server(f"{base_url}{object_path}")

    def _server(self, url: str) -> Any:
        if self.proxy_factory is not xmlrpc.client.ServerProxy:
            return self.proxy_factory(url)
        transport = _BoundedSafeTransport() if url.startswith("https:") else _BoundedTransport()
        return self.proxy_factory(url, transport=transport, allow_none=True)

    def _login(self, common: Any) -> int:
        uid = common.authenticate(
            self.database, self.config["username"], self.password, {}
        )
        if not uid:
            raise ValueError("ERP authentication failed for the configured read-only user.")
        return int(uid)

    def _execute(self, model: str, method: str, *args: Any, **kwargs: Any) -> Any:
        return self.object_proxy.execute_kw(
            self.database, self.uid, self.password, model, method, list(args), kwargs
        )

    def fields(self, model: str) -> set[str]:
        result = self._execute(model, "fields_get")
        return set(result)

    def search(self, model: str, domain: list, *, offset: int = 0) -> list[int]:
        return self._execute(
            model,
            "search",
            domain,
            offset=offset,
            limit=PAGE_SIZE,
            order="id asc",
        ) or []

    def read(self, model: str, ids: list[int], field_names: list[str]) -> list[dict]:
        if not ids:
            return []
        rows = self._execute(model, "read", ids, fields=field_names)
        return rows if isinstance(rows, list) else [rows]

    def search_read_pages(
        self, model: str, domain: list, field_names: list[str]
    ) -> Iterator[dict]:
        offset = 0
        while True:
            ids = self.search(model, domain, offset=offset)
            if not ids:
                return
            yield from self.read(model, ids, field_names)
            offset += len(ids)
            if len(ids) < PAGE_SIZE:
                return

    def read_group(
        self, model: str, domain: list, fields: list[str], groupby: list[str]
    ) -> list[dict[str, Any]] | None:
        try:
            result = self._execute(
                model,
                "read_group",
                domain,
                fields,
                groupby,
                lazy=False,
            )
        except Exception:
            return None
        return result if isinstance(result, list) else None


def _relation_id(value: Any) -> int | None:
    if isinstance(value, (list, tuple)) and value:
        return int(value[0]) if value[0] else None
    if isinstance(value, int):
        return value or None
    return None


class OpenERPReader:
    """Read-only OpenERP 5 account and journal-item adapter backed by erppeek."""

    def __init__(self, client: ErpPeekReaderClient):
        self.client = client

    def accounts(self, company_id: int) -> list[dict[str, Any]]:
        fields = self.client.fields("account.account")
        requested = [name for name in ("id", "code", "name", "odoo_account_code") if name in fields]
        rows = list(
            self.client.search_read_pages(
                "account.account", [("company_id", "=", company_id)], requested
            )
        )
        return [
            row
            for row in rows
            if len(str(row.get("code") or "")) == 12
            and str(row.get("code") or "").isdigit()
        ]

    def period_totals(
        self, company_id: int, date_from: date, date_to: date, state: str
    ) -> dict[str, tuple[Decimal, Decimal]]:
        """Aggregate posted AMLs inside OpenERP's indexed account function field."""
        if state != "posted":
            raise ValueError("OpenERP 5 aggregate totals support posted entries only.")
        accounts = self.accounts(company_id)
        account_ids = [int(account["id"]) for account in accounts]
        account_by_id = {int(account["id"]): str(account.get("code") or "") for account in accounts}
        if not account_ids:
            return {}
        period_ids = self.client.search(
            "account.period",
            [
                ("date_start", "<=", date_to.isoformat()),
                ("date_stop", ">=", date_from.isoformat()),
            ],
        )
        if not period_ids:
            return {code: (Decimal("0.00"), Decimal("0.00")) for code in account_by_id.values()}
        account_fields = self.client.fields("account.account")
        requested = [name for name in ("id", "debit", "credit") if name in account_fields]
        context = {
            "periods": period_ids,
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "state": "bydate",
        }
        totals = {}
        for offset in range(0, len(account_ids), PAGE_SIZE):
            rows = self.client.read(
                "account.account",
                account_ids[offset:offset + PAGE_SIZE],
                requested,
                context=context,
            )
            for row in rows:
                code = account_by_id[int(row["id"])]
                totals[code] = (
                    decimal_amount(row.get("debit")),
                    decimal_amount(row.get("credit")),
                )
        return totals

    def sync_operations(
        self, company_id: int, date_from: date, date_to: date, state: str
    ) -> list[dict[str, Any]]:
        """Classify invoice and entry source identities without reading every AML."""
        if state != "posted":
            return []
        journals = list(self.client.search_read_pages(
            "account.journal",
            [("company_id", "=", company_id)],
            ["id", "name", "som_sync_odoo_invoices", "som_sync_odoo_account_moves"],
        ))
        journals_by_id = {int(item["id"]): item for item in journals}
        company_journal_ids = sorted(journals_by_id)
        invoices = list(self.client.search_read_pages(
            "account.invoice",
            [
                ("journal_id", "in", company_journal_ids),
                ("date_invoice", ">=", date_from.isoformat()),
                ("date_invoice", "<=", date_to.isoformat()),
                ("state", "in", ["open", "paid"]),
            ],
            ["id", "move_id", "journal_id", "date_invoice", "state", "number"],
        ))
        operations = []
        invoice_move_ids = set()
        for invoice in invoices:
            journal = journals_by_id.get(_relation_id(invoice.get("journal_id")) or -1, {})
            move_id = _relation_id(invoice.get("move_id"))
            if journal.get("som_sync_odoo_invoices"):
                operations.append({
                    "source_model": "account.invoice",
                    "source_id": int(invoice["id"]),
                    "move_id": move_id,
                    "date": str(invoice.get("date_invoice") or "")[:10],
                    "label": str(invoice.get("number") or ""),
                    "journal": str(journal.get("name") or ""),
                    "sync_classification": "invoice",
                })
            if move_id:
                invoice_move_ids.add(move_id)

        moves = list(self.client.search_read_pages(
            "account.move",
            [
                ("journal_id", "in", company_journal_ids),
                ("date", ">=", date_from.isoformat()),
                ("date", "<=", date_to.isoformat()),
                ("state", "in", ["posted", "valid"]),
            ],
            ["id", "name", "date", "state", "journal_id"],
        ))
        candidates = [
            move for move in moves
            if int(move["id"]) not in invoice_move_ids
            and journals_by_id.get(_relation_id(move.get("journal_id")) or -1, {}).get("som_sync_odoo_account_moves")
        ]
        bank_account_ids = [
            int(account["id"])
            for account in self.accounts(company_id)
            if str(account.get("code") or "").startswith("572")
        ]
        blocked_move_ids: set[int] = set()
        for account_offset in range(0, len(bank_account_ids), ERP_ACCOUNT_BATCH_SIZE):
            domain = [
                ("account_id", "in", bank_account_ids[account_offset:account_offset + ERP_ACCOUNT_BATCH_SIZE]),
                ("date", ">=", date_from.isoformat()),
                ("date", "<=", date_to.isoformat()),
            ]
            for line in self.client.search_read_pages("account.move.line", domain, ["move_id"]):
                move_id = _relation_id(line.get("move_id"))
                if move_id:
                    blocked_move_ids.add(move_id)
        for move in candidates:
            move_id = int(move["id"])
            journal = journals_by_id.get(_relation_id(move.get("journal_id")) or -1, {})
            if move_id in blocked_move_ids:
                operations.append({
                    "source_model": "account.move",
                    "source_id": move_id,
                    "move_id": move_id,
                    "date": str(move.get("date") or "")[:10],
                    "label": str(move.get("name") or ""),
                    "journal": str(journal.get("name") or ""),
                    "sync_classification": "excluded",
                    "reason": "Direct general-entry synchronization excludes moves with 572 accounts.",
                })
                continue
            operations.append({
                "source_model": "account.move",
                "source_id": move_id,
                "move_id": move_id,
                "date": str(move.get("date") or "")[:10],
                "label": str(move.get("name") or ""),
                "journal": str(journal.get("name") or ""),
                "sync_classification": "entry",
            })
        return operations

    def period_totals(
        self, company_id: int, date_from: date, date_to: date, state: str
    ) -> dict[str, tuple[Decimal, Decimal]]:
        """Read grouped period debit/credit via OpenERP's account function fields."""
        if state != "posted":
            raise ValueError("OpenERP 5 aggregate totals support posted entries only.")
        accounts = self.accounts(company_id)
        account_ids = [int(account["id"]) for account in accounts]
        account_by_id = {int(account["id"]): str(account.get("code") or "") for account in accounts}
        if not account_ids:
            return {}
        period_ids = self.client.search(
            "account.period",
            [
                ("date_start", "<=", date_to.isoformat()),
                ("date_stop", ">=", date_from.isoformat()),
            ],
        )
        if not period_ids:
            return {}
        fields = self.client.fields("account.account")
        requested = [name for name in ("id", "debit", "credit") if name in fields]
        context = {
            "periods": period_ids,
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "state": "bydate",
        }
        totals = {}
        for offset in range(0, len(account_ids), PAGE_SIZE):
            rows = self.client.read(
                "account.account",
                account_ids[offset:offset + PAGE_SIZE],
                requested,
                context=context,
            )
            for row in rows:
                account_id = int(row["id"])
                totals[account_by_id[account_id]] = (
                    decimal_amount(row.get("debit")),
                    decimal_amount(row.get("credit")),
                )
        return totals

    def sync_operations(
        self, company_id: int, date_from: date, date_to: date, state: str
    ) -> list[dict[str, Any]]:
        """Classify eligible invoice/entry origins without reading all AML details."""
        if state != "posted":
            return []
        accounts = self.accounts(company_id)
        journal_fields = self.client.fields("account.journal")
        journal_flags = [
            name for name in ("id", "name", "som_sync_odoo_invoices", "som_sync_odoo_account_moves")
            if name in journal_fields
        ]
        journal_by_id = {
            int(journal["id"]): journal
            for journal in self.client.search_read_pages(
                "account.journal", [("company_id", "=", company_id)], journal_flags
            )
        }
        invoice_journal_ids = sorted(
            journal_id for journal_id, journal in journal_by_id.items()
            if journal.get("som_sync_odoo_invoices")
        )
        entry_journal_ids = sorted(
            journal_id for journal_id, journal in journal_by_id.items()
            if journal.get("som_sync_odoo_account_moves")
        )
        invoices = []
        invoice_fields = self.client.fields("account.invoice")
        invoice_requested = [
            name for name in ("id", "move_id", "journal_id", "date_invoice", "state", "number")
            if name in invoice_fields
        ]
        if "date_invoice" in invoice_fields and invoice_journal_ids:
            invoices = list(self.client.search_read_pages(
                "account.invoice",
                [
                    ("journal_id", "in", invoice_journal_ids),
                    ("date_invoice", ">=", date_from.isoformat()),
                    ("date_invoice", "<=", date_to.isoformat()),
                    ("state", "in", ["open", "paid"]),
                ],
                invoice_requested,
            ))
        operations = []
        invoice_move_ids = set()
        for invoice in invoices:
            journal_id = _relation_id(invoice.get("journal_id"))
            journal = journal_by_id.get(journal_id or -1, {})
            move_id = _relation_id(invoice.get("move_id"))
            if move_id:
                invoice_move_ids.add(move_id)
            if invoice.get("state") in {"open", "paid"} and journal.get("som_sync_odoo_invoices"):
                operations.append({
                    "source_model": "account.invoice",
                    "source_id": int(invoice["id"]),
                    "move_id": move_id,
                    "date": str(invoice.get("date_invoice") or "")[:10],
                    "label": str(invoice.get("number") or ""),
                    "journal": str(journal.get("name") or ""),
                    "sync_classification": "invoice",
                })

        move_fields = self.client.fields("account.move")
        move_requested = [name for name in ("id", "name", "date", "state", "journal_id") if name in move_fields]
        moves = []
        if entry_journal_ids:
            moves = list(self.client.search_read_pages(
                "account.move",
                [
                    ("journal_id", "in", entry_journal_ids),
                    ("date", ">=", date_from.isoformat()),
                    ("date", "<=", date_to.isoformat()),
                    ("state", "in", ["posted", "valid"]),
                ],
                move_requested,
            ))
        entry_candidates = [
            move for move in moves
            if int(move["id"]) not in invoice_move_ids
            and journal_by_id.get(_relation_id(move.get("journal_id")) or -1, {}).get("som_sync_odoo_account_moves")
        ]
        bank_account_ids = [
            int(account["id"])
            for account in accounts
            if str(account.get("code") or "").startswith("572")
        ]
        blocked_move_ids: set[int] = set()
        if bank_account_ids:
            for offset in range(0, len(bank_account_ids), ERP_ACCOUNT_BATCH_SIZE):
                lines = self.client.search_read_pages(
                    "account.move.line",
                    [
                        ("account_id", "in", bank_account_ids[offset:offset + ERP_ACCOUNT_BATCH_SIZE]),
                        ("date", ">=", date_from.isoformat()),
                        ("date", "<=", date_to.isoformat()),
                    ],
                    ["move_id"],
                )
                blocked_move_ids.update(
                    move_id for line in lines if (move_id := _relation_id(line.get("move_id")))
                )
        for move in entry_candidates:
            move_id = int(move["id"])
            if move_id in blocked_move_ids:
                continue
            journal = journal_by_id.get(_relation_id(move.get("journal_id")) or -1, {})
            operations.append({
                "source_model": "account.move",
                "source_id": move_id,
                "move_id": move_id,
                "date": str(move.get("date") or "")[:10],
                "label": str(move.get("name") or ""),
                "journal": str(journal.get("name") or ""),
                "sync_classification": "entry",
            })
        return operations

    def journal_items(
        self,
        company_id: int,
        date_from: date,
        date_to: date,
        state: str,
        account_codes: set[str] | None = None,
    ) -> list[JournalItem]:
        accounts = self.accounts(company_id)
        if account_codes is not None:
            accounts = [account for account in accounts if account.get("code") in account_codes]
        account_ids = [int(item["id"]) for item in accounts]
        account_by_id = {int(item["id"]): item for item in accounts}
        if not account_ids:
            return []
        line_fields = self.client.fields("account.move.line")
        wanted = (
            "id", "move_id", "account_id", "date", "debit", "credit", "name", "ref",
            "journal_id",
        )
        fields = [name for name in wanted if name in line_fields]
        raw_lines = []
        for offset in range(0, len(account_ids), ERP_ACCOUNT_BATCH_SIZE):
            domain = [
                ("account_id", "in", account_ids[offset:offset + ERP_ACCOUNT_BATCH_SIZE]),
                ("date", ">=", date_from.isoformat()),
                ("date", "<=", date_to.isoformat()),
            ]
            raw_lines.extend(self.client.search_read_pages("account.move.line", domain, fields))
        move_ids = sorted({mid for line in raw_lines if (mid := _relation_id(line.get("move_id")))})
        move_by_id: dict[int, dict] = {}
        if move_ids:
            move_fields = self.client.fields("account.move")
            move_requested = [name for name in ("id", "name", "state", "journal_id", "ref") if name in move_fields]
            move_by_id = {
                int(move["id"]): move
                for move in self.client.read("account.move", move_ids, move_requested)
            }
        invoice_by_move_id: dict[int, dict] = {}
        invoice_fields = self.client.fields("account.invoice")
        invoice_requested = [
            name for name in ("id", "move_id", "journal_id", "date_invoice", "state")
            if name in invoice_fields
        ]
        if "move_id" in invoice_fields and move_ids:
            for offset in range(0, len(move_ids), ERP_ACCOUNT_BATCH_SIZE):
                invoice_domain = [("move_id", "in", move_ids[offset:offset + ERP_ACCOUNT_BATCH_SIZE])]
                for invoice in self.client.search_read_pages(
                    "account.invoice", invoice_domain, invoice_requested
                ):
                    invoice_move_id = _relation_id(invoice.get("move_id"))
                    if invoice_move_id:
                        invoice_by_move_id[invoice_move_id] = invoice
        journal_ids = sorted(
            {
                _relation_id(line.get("journal_id")) or _relation_id(move_by_id.get(_relation_id(line.get("move_id")) or -1, {}).get("journal_id"))
                for line in raw_lines
            }
            - {None}
        )
        journal_by_id: dict[int, dict] = {}
        if journal_ids:
            journal_fields = self.client.fields("account.journal")
            journal_requested = [
                name for name in (
                    "id", "name", "code", "som_sync_odoo_invoices",
                    "som_sync_odoo_account_moves",
                ) if name in journal_fields
            ]
            journal_by_id = {
                int(journal["id"]): journal
                for journal in self.client.read("account.journal", journal_ids, journal_requested)
            }

        moves_with_572 = set()
        for row in raw_lines:
            if str(account_by_id.get(_relation_id(row.get("account_id")) or -1, {}).get("code") or "").startswith("572"):
                moves_with_572.add(_relation_id(row.get("move_id")))

        result = []
        for row in raw_lines:
            move_id = _relation_id(row.get("move_id")) or 0
            move = move_by_id.get(move_id, {})
            if state == "posted" and move.get("state") not in {"posted", "valid"}:
                continue
            account_id = _relation_id(row.get("account_id")) or 0
            account = account_by_id.get(account_id, {})
            journal_id = _relation_id(row.get("journal_id")) or _relation_id(move.get("journal_id"))
            journal = journal_by_id.get(journal_id or -1, {})
            invoice = invoice_by_move_id.get(move_id)
            invoice_journal = journal_by_id.get(_relation_id(invoice.get("journal_id")) or -1, {}) if invoice else {}
            has_572_account = move_id in moves_with_572
            invoice_sync_eligible = bool(
                invoice
                and invoice.get("state") in {"open", "paid"}
                and invoice_journal.get("som_sync_odoo_invoices")
            )
            entry_journal_enabled = bool(journal.get("som_sync_odoo_account_moves"))
            entry_sync_eligible = bool(
                move.get("state") in {"posted", "valid"}
                and entry_journal_enabled
                and not has_572_account
            )
            route_ambiguous = invoice_sync_eligible and entry_sync_eligible
            if route_ambiguous:
                sync_classification = "ambiguous"
                sync_reason = "The invoice and general-entry synchronization rules both apply."
            elif invoice_sync_eligible:
                sync_classification, sync_reason = "invoice", ""
            elif entry_sync_eligible:
                sync_classification, sync_reason = "entry", ""
            elif entry_journal_enabled and has_572_account:
                sync_classification, sync_reason = "excluded", "Direct general-entry synchronization excludes 572 accounts."
            else:
                sync_classification, sync_reason = "excluded", "No enabled synchronization route applies to this journal operation."
            source_model = (
                "account.invoice" if invoice else "account.move"
            )
            result.append(
                JournalItem(
                    system="source",
                    line_id=int(row["id"]),
                    move_id=move_id,
                    account_code=str(account.get("code") or ""),
                    account_name=str(account.get("name") or ""),
                    date=date.fromisoformat(str(row["date"])[:10]),
                    debit=decimal_amount(row.get("debit")),
                    credit=decimal_amount(row.get("credit")),
                    journal=str(journal.get("name") or journal.get("code") or ""),
                    move_label=str(move.get("name") or ""),
                    description=str(row.get("ref") or row.get("name") or ""),
                    source_model=source_model,
                    source_id=int(invoice["id"]) if invoice else move_id,
                    sync_eligible=(invoice_sync_eligible or entry_sync_eligible) and not route_ambiguous,
                    sync_classification=sync_classification,
                    sync_reason=sync_reason,
                )
            )
        return result

    def cumulative_totals(
        self, company_id: int, date_to: date, state: str
    ) -> dict[str, tuple[Decimal, Decimal]] | None:
        if getattr(self.client.client, "major_version", "") == "5.0":
            try:
                return self.period_totals(company_id, date(1900, 1, 1), date_to, state)
            except Exception:
                # Cumulative data is informational; it must not block period movement.
                return None
        accounts = self.accounts(company_id)
        account_ids = [int(account["id"]) for account in accounts]
        if not account_ids:
            return {}
        account_by_id = {int(account["id"]): str(account.get("code") or "") for account in accounts}
        domain = [("account_id", "in", account_ids), ("date", "<=", date_to.isoformat())]
        if state == "posted":
            domain.append(("move_id.state", "=", "posted"))
        totals = {}
        fields = self.client.fields("account.move.line")
        requested = [name for name in ("account_id", "debit", "credit") if name in fields]
        for offset in range(0, len(account_ids), ERP_ACCOUNT_BATCH_SIZE):
            batch_ids = account_ids[offset:offset + ERP_ACCOUNT_BATCH_SIZE]
            domain = [("account_id", "in", batch_ids), ("date", "<=", date_to.isoformat())]
            if state == "posted":
                domain.append(("move_id.state", "=", "posted"))
            groups = self.client.read_group(
                "account.move.line", domain, ["account_id", "debit", "credit"], ["account_id"]
            )
            if groups is None:
                try:
                    for row in self.client.search_read_pages("account.move.line", domain, requested):
                        account_id = _relation_id(row.get("account_id"))
                        if account_id not in account_by_id:
                            continue
                        code = account_by_id[account_id]
                        debit, credit = totals.get(code, (Decimal("0.00"), Decimal("0.00")))
                        totals[code] = (debit + decimal_amount(row.get("debit")), credit + decimal_amount(row.get("credit")))
                except Exception:
                    return None
            else:
                for row in groups:
                    account_id = _relation_id(row.get("account_id"))
                    if account_id in account_by_id:
                        code = account_by_id[account_id]
                        debit, credit = totals.get(code, (Decimal("0.00"), Decimal("0.00")))
                        totals[code] = (debit + decimal_amount(row.get("debit")), credit + decimal_amount(row.get("credit")))
        return totals

    def payment_operations(self, company_id: int) -> list[dict[str, Any]]:
        order_fields = self.client.fields("payment.order")
        required = {"id", "state", "paid", "mode"}
        if not required.issubset(order_fields):
            return []
        wanted = [
            name for name in (
                "id", "name", "state", "paid", "mode", "company_id", "date_done",
                "date_created", "date_planned",
            ) if name in order_fields
        ]
        if "company_id" not in order_fields:
            return []
        domain = [
            ("company_id", "=", company_id),
            ("state", "=", "done"),
            ("paid", "=", True),
        ]
        orders = list(self.client.search_read_pages("payment.order", domain, wanted))
        mode_ids = sorted({_relation_id(order.get("mode")) for order in orders} - {None})
        mode_by_id: dict[int, dict] = {}
        if mode_ids:
            mode_fields = self.client.fields("payment.mode")
            mode_wanted = [name for name in ("id", "name", "som_sync_odoo") if name in mode_fields]
            mode_by_id = {
                int(mode["id"]): mode
                for mode in self.client.read("payment.mode", mode_ids, mode_wanted)
            }
        result = []
        for order in orders:
            mode = mode_by_id.get(_relation_id(order.get("mode")) or -1, {})
            if order.get("state") != "done" or not order.get("paid") or not mode.get("som_sync_odoo"):
                continue
            result.append({
                "source_model": "payment.order",
                "source_id": int(order["id"]),
                "label": str(order.get("name") or ""),
                "date": str(order.get("date_done") or order.get("date_planned") or order.get("date_created") or "")[:10],
                "payment_mode": str(mode.get("name") or ""),
            })
        return result


class OdooReader:
    """Read-only Odoo 18 account and journal-item adapter."""

    def __init__(self, client: OdooXMLRPCReader):
        self.client = client

    def accounts(self, company_id: int) -> list[dict[str, Any]]:
        fields = self.client.fields("account.account")
        requested = [name for name in ("id", "code", "code_store", "name") if name in fields]
        return list(
            self.client.search_read_pages(
                "account.account", [("company_ids", "in", [company_id])], requested
            )
        )

    def journal_items(
        self,
        company_id: int,
        date_from: date,
        date_to: date,
        state: str,
        account_codes: set[str] | None = None,
    ) -> list[JournalItem]:
        account_rows = self.accounts(company_id)
        if account_codes is not None:
            account_rows = [
                account for account in account_rows
                if str(account.get("code_store") or account.get("code") or "") in account_codes
            ]
        account_by_id = {int(item["id"]): item for item in account_rows}
        if not account_by_id:
            return []
        fields = self.client.fields("account.move.line")
        wanted = (
            "id", "move_id", "account_id", "date", "debit", "credit", "name", "ref",
            "journal_id", "parent_state",
        )
        requested = [name for name in wanted if name in fields]
        domain = [
            ("company_id", "=", company_id),
            ("date", ">=", date_from.isoformat()),
            ("date", "<=", date_to.isoformat()),
            *( [("account_id", "in", list(account_by_id))] if account_codes is not None else [] ),
        ]
        if state == "posted":
            domain.append(("parent_state", "=", "posted"))
        raw_lines = list(self.client.search_read_pages("account.move.line", domain, requested))
        move_ids = sorted({mid for line in raw_lines if (mid := _relation_id(line.get("move_id")))})
        move_by_id: dict[int, dict] = {}
        if move_ids:
            move_fields = self.client.fields("account.move")
            move_requested = [name for name in ("id", "name", "state", "ref", "pnt_erp_id", "move_type", "reversed_entry_id") if name in move_fields]
            move_by_id = {
                int(move["id"]): move
                for move in self.client.read("account.move", move_ids, move_requested)
            }
            reversed_ids = sorted(
                {
                    original_id
                    for move in move_by_id.values()
                    if (original_id := _relation_id(move.get("reversed_entry_id")))
                    and original_id not in move_by_id
                }
            )
            if reversed_ids:
                original_rows = self.client.read("account.move", reversed_ids, move_requested)
                move_by_id.update({int(move["id"]): move for move in original_rows})
        statement_source_by_move: dict[int, tuple[str, int]] = {}
        statement_fields = self.client.fields("account.bank.statement.line")
        statement_wanted = [
            name for name in ("id", "move_id", "pnt_source_model", "pnt_erp_id")
            if name in statement_fields
        ]
        if {"move_id", "pnt_source_model", "pnt_erp_id"}.issubset(statement_fields) and move_ids:
            for offset in range(0, len(move_ids), PAGE_SIZE):
                statement_domain = [
                    ("company_id", "=", company_id),
                    ("move_id", "in", move_ids[offset:offset + PAGE_SIZE]),
                    ("pnt_erp_id", ">", 0),
                ]
                for statement in self.client.search_read_pages(
                    "account.bank.statement.line", statement_domain, statement_wanted
                ):
                    statement_move_id = _relation_id(statement.get("move_id"))
                    if statement_move_id and statement.get("pnt_source_model") and statement.get("pnt_erp_id"):
                        statement_source_by_move[statement_move_id] = (
                            str(statement["pnt_source_model"]), int(statement["pnt_erp_id"])
                        )
        payment_source_by_move = self._payment_move_origins(company_id, move_ids)
        journal_ids = sorted({_relation_id(line.get("journal_id")) for line in raw_lines} - {None})
        journal_by_id: dict[int, dict] = {}
        if journal_ids:
            journal_fields = self.client.fields("account.journal")
            journal_requested = [name for name in ("id", "name", "code") if name in journal_fields]
            journal_by_id = {
                int(journal["id"]): journal
                for journal in self.client.read("account.journal", journal_ids, journal_requested)
            }

        result = []
        for row in raw_lines:
            move_id = _relation_id(row.get("move_id")) or 0
            move = move_by_id.get(move_id, {})
            original_move_id = _relation_id(move.get("reversed_entry_id"))
            origin_move = move_by_id.get(original_move_id or -1, move)
            statement_origin = statement_source_by_move.get(move_id)
            payment_origin = payment_source_by_move.get(move_id)
            account = account_by_id.get(_relation_id(row.get("account_id")) or -1, {})
            journal = journal_by_id.get(_relation_id(row.get("journal_id")) or -1, {})
            result.append(
                JournalItem(
                    system="target",
                    line_id=int(row["id"]),
                    move_id=move_id,
                    account_code=str(account.get("code_store") or account.get("code") or ""),
                    account_name=str(account.get("name") or ""),
                    date=date.fromisoformat(str(row["date"])[:10]),
                    debit=decimal_amount(row.get("debit")),
                    credit=decimal_amount(row.get("credit")),
                    journal=str(journal.get("name") or journal.get("code") or ""),
                    move_label=str(move.get("name") or ""),
                    description=str(row.get("ref") or row.get("name") or ""),
                    source_model=(
                        statement_origin[0]
                        if statement_origin
                        else "payment.order"
                        if payment_origin
                        else ("account.move" if origin_move.get("move_type") == "entry" else "account.invoice")
                        if origin_move.get("pnt_erp_id")
                        else ""
                    ),
                    source_id=(
                        statement_origin[1]
                        if statement_origin
                        else payment_origin
                        if payment_origin
                        else int(origin_move["pnt_erp_id"])
                        if origin_move.get("pnt_erp_id")
                        else None
                    ),
                    is_native=not bool(statement_origin or payment_origin or origin_move.get("pnt_erp_id")),
                    sync_eligible=bool(statement_origin or payment_origin or origin_move.get("pnt_erp_id")),
                    is_reversal=bool(original_move_id and origin_move.get("pnt_erp_id")),
                )
            )
        return result

    def period_totals(
        self, company_id: int, date_from: date, date_to: date, state: str
    ) -> tuple[
        dict[str, tuple[Decimal, Decimal]],
        dict[str, tuple[Decimal, Decimal]],
    ]:
        """Aggregate the full ledger and lines with a proven ERP-origin move."""
        accounts = self.accounts(company_id)
        account_by_id = {
            int(account["id"]): str(account.get("code_store") or account.get("code") or "")
            for account in accounts
        }
        base_domain = [
            ("company_id", "=", company_id),
            ("date", ">=", date_from.isoformat()),
            ("date", "<=", date_to.isoformat()),
        ]
        if state == "posted":
            base_domain.append(("parent_state", "=", "posted"))

        def totals_for(domain: list) -> dict[str, tuple[Decimal, Decimal]]:
            groups = self.client.read_group(
                "account.move.line",
                domain,
                ["account_id", "debit", "credit"],
                ["account_id"],
            )
            if groups is None:
                raise RuntimeError("Odoo account movement aggregation failed.")
            result = {}
            for group in groups:
                account_id = _relation_id(group.get("account_id"))
                code = account_by_id.get(account_id or -1)
                if code:
                    result[code] = (
                        decimal_amount(group.get("debit")),
                        decimal_amount(group.get("credit")),
                    )
            return result

        total = totals_for(base_domain)
        move_ids: set[int] = set()
        move_fields = self.client.fields("account.move")
        if "pnt_erp_id" in move_fields:
            move_domain = [
                ("company_id", "=", company_id),
                ("date", ">=", date_from.isoformat()),
                ("date", "<=", date_to.isoformat()),
                ("pnt_erp_id", ">", 0),
            ]
            if state == "posted":
                move_domain.append(("state", "=", "posted"))
            move_ids.update(
                int(row["id"])
                for row in self.client.search_read_pages("account.move", move_domain, ["id"])
            )

        statement_fields = self.client.fields("account.bank.statement.line")
        if {"pnt_source_model", "pnt_erp_id", "move_id"}.issubset(statement_fields):
            statement_domain = [
                ("company_id", "=", company_id),
                ("date", ">=", date_from.isoformat()),
                ("date", "<=", date_to.isoformat()),
                ("pnt_erp_id", ">", 0),
            ]
            for row in self.client.search_read_pages(
                "account.bank.statement.line", statement_domain, ["move_id"]
            ):
                move_id = _relation_id(row.get("move_id"))
                if move_id:
                    move_ids.add(move_id)

        batch_fields = self.client.fields("account.batch.payment")
        if {"pnt_erp_id", "payment_ids"}.issubset(batch_fields):
            batch_domain = [
                ("company_id", "=", company_id),
                ("date", ">=", date_from.isoformat()),
                ("date", "<=", date_to.isoformat()),
                ("pnt_erp_id", ">", 0),
            ]
            payment_ids = sorted({
                int(payment_id)
                for batch in self.client.search_read_pages(
                    "account.batch.payment", batch_domain, ["payment_ids"]
                )
                for payment_id in (batch.get("payment_ids") or [])
            })
            payment_fields = self.client.fields("account.payment")
            if payment_ids and "move_id" in payment_fields:
                move_ids.update(
                    move_id
                    for payment in self.client.read("account.payment", payment_ids, ["move_id"])
                    if (move_id := _relation_id(payment.get("move_id")))
                )

        synced: dict[str, tuple[Decimal, Decimal]] = {}
        ordered_move_ids = sorted(move_ids)
        for offset in range(0, len(ordered_move_ids), PAGE_SIZE):
            domain = base_domain + [("move_id", "in", ordered_move_ids[offset:offset + PAGE_SIZE])]
            for code, (debit, credit) in totals_for(domain).items():
                previous_debit, previous_credit = synced.get(code, (Decimal("0.00"), Decimal("0.00")))
                synced[code] = (previous_debit + debit, previous_credit + credit)
        return total, synced

    def period_totals(
        self, company_id: int, date_from: date, date_to: date, state: str
    ) -> tuple[
        dict[str, tuple[Decimal, Decimal]],
        dict[str, tuple[Decimal, Decimal]],
    ]:
        """Aggregate target movements and target moves with proven ERP origins."""
        accounts = self.accounts(company_id)
        account_by_id = {
            int(account["id"]): str(account.get("code_store") or account.get("code") or "")
            for account in accounts
        }
        base_domain = [
            ("company_id", "=", company_id),
            ("date", ">=", date_from.isoformat()),
            ("date", "<=", date_to.isoformat()),
        ]
        if state == "posted":
            base_domain.append(("parent_state", "=", "posted"))

        def grouped_totals(domain: list) -> dict[str, tuple[Decimal, Decimal]]:
            groups = self.client.read_group(
                "account.move.line",
                domain,
                ["account_id", "debit", "credit"],
                ["account_id"],
            )
            if groups is None:
                raise RuntimeError("Odoo read_group failed for account.move.line.")
            totals = {}
            for row in groups:
                account_id = _relation_id(row.get("account_id"))
                code = account_by_id.get(account_id or -1)
                if code:
                    totals[code] = (
                        decimal_amount(row.get("debit")),
                        decimal_amount(row.get("credit")),
                    )
            return totals

        total = grouped_totals(base_domain)
        move_ids: set[int] = set()

        move_fields = self.client.fields("account.move")
        if "pnt_erp_id" in move_fields:
            move_domain = [
                ("company_id", "=", company_id),
                ("date", ">=", date_from.isoformat()),
                ("date", "<=", date_to.isoformat()),
                ("pnt_erp_id", ">", 0),
            ]
            if state == "posted":
                move_domain.append(("state", "=", "posted"))
            move_ids.update(
                int(row["id"])
                for row in self.client.search_read_pages("account.move", move_domain, ["id"])
            )

        statement_fields = self.client.fields("account.bank.statement.line")
        if {"pnt_source_model", "pnt_erp_id", "move_id"}.issubset(statement_fields):
            statements = self.client.search_read_pages(
                "account.bank.statement.line",
                [
                    ("company_id", "=", company_id),
                    ("date", ">=", date_from.isoformat()),
                    ("date", "<=", date_to.isoformat()),
                    ("pnt_erp_id", ">", 0),
                ],
                ["move_id"],
            )
            move_ids.update(
                move_id for row in statements if (move_id := _relation_id(row.get("move_id")))
            )

        batch_fields = self.client.fields("account.batch.payment")
        if {"pnt_erp_id", "payment_ids"}.issubset(batch_fields):
            batches = self.client.search_read_pages(
                "account.batch.payment",
                [
                    ("company_id", "=", company_id),
                    ("date", ">=", date_from.isoformat()),
                    ("date", "<=", date_to.isoformat()),
                    ("pnt_erp_id", ">", 0),
                ],
                ["payment_ids"],
            )
            payment_ids = sorted({payment_id for batch in batches for payment_id in (batch.get("payment_ids") or [])})
            payment_fields = self.client.fields("account.payment")
            if payment_ids and "move_id" in payment_fields:
                payments = self.client.read("account.payment", payment_ids, ["move_id"])
                move_ids.update(
                    move_id for payment in payments if (move_id := _relation_id(payment.get("move_id")))
                )

        synced: dict[str, tuple[Decimal, Decimal]] = {}
        move_id_list = sorted(move_ids)
        for offset in range(0, len(move_id_list), PAGE_SIZE):
            ids = move_id_list[offset:offset + PAGE_SIZE]
            for code, (debit, credit) in grouped_totals(base_domain + [("move_id", "in", ids)]).items():
                old_debit, old_credit = synced.get(code, (Decimal("0.00"), Decimal("0.00")))
                synced[code] = (old_debit + debit, old_credit + credit)
        return total, synced

    def _payment_move_origins(
        self, company_id: int, selected_move_ids: list[int]
    ) -> dict[int, int]:
        batch_fields = self.client.fields("account.batch.payment")
        if not {"pnt_erp_id", "payment_ids"}.issubset(batch_fields) or not selected_move_ids:
            return {}
        batches = list(
            self.client.search_read_pages(
                "account.batch.payment",
                [("company_id", "=", company_id), ("pnt_erp_id", ">", 0)],
                ["id", "pnt_erp_id", "payment_ids"],
            )
        )
        payment_to_order = {
            int(payment_id): int(batch["pnt_erp_id"])
            for batch in batches
            for payment_id in (batch.get("payment_ids") or [])
        }
        if not payment_to_order:
            return {}
        payment_fields = self.client.fields("account.payment")
        if "move_id" not in payment_fields:
            return {}
        payments = self.client.read(
            "account.payment",
            sorted(payment_to_order),
            [name for name in ("id", "move_id") if name in payment_fields],
        )
        move_to_order = {}
        selected = set(selected_move_ids)
        for payment in payments:
            move_id = _relation_id(payment.get("move_id"))
            if move_id in selected:
                move_to_order[move_id] = payment_to_order[int(payment["id"])]
        return move_to_order

    def cumulative_totals(
        self, company_id: int, date_to: date, state: str
    ) -> dict[str, tuple[Decimal, Decimal]] | None:
        accounts = self.accounts(company_id)
        account_by_id = {
            int(account["id"]): str(account.get("code_store") or account.get("code") or "")
            for account in accounts
        }
        domain = [("company_id", "=", company_id), ("date", "<=", date_to.isoformat())]
        if state == "posted":
            domain.append(("parent_state", "=", "posted"))
        groups = self.client.read_group(
            "account.move.line", domain, ["account_id", "debit", "credit"], ["account_id"]
        )
        totals = {}
        if groups is None:
            try:
                fields = self.client.fields("account.move.line")
                requested = [name for name in ("account_id", "debit", "credit") if name in fields]
                for row in self.client.search_read_pages("account.move.line", domain, requested):
                    account_id = _relation_id(row.get("account_id"))
                    if account_id not in account_by_id:
                        continue
                    code = account_by_id[account_id]
                    debit, credit = totals.get(code, (Decimal("0.00"), Decimal("0.00")))
                    totals[code] = (debit + decimal_amount(row.get("debit")), credit + decimal_amount(row.get("credit")))
            except Exception:
                return None
        else:
            for row in groups:
                account_id = _relation_id(row.get("account_id"))
                if account_id in account_by_id:
                    totals[account_by_id[account_id]] = (
                        decimal_amount(row.get("debit")), decimal_amount(row.get("credit"))
                    )
        return totals

    def origins(
        self,
        company_id: int,
        candidates: set[tuple[str, int]] | None = None,
    ) -> dict[tuple[str, int], list[dict[str, Any]]]:
        result: dict[tuple[str, int], list[dict[str, Any]]] = {}
        move_fields = self.client.fields("account.move")
        if {"pnt_erp_id", "move_type"}.issubset(move_fields):
            wanted = [name for name in ("id", "pnt_erp_id", "move_type", "date", "name", "state") if name in move_fields]
            if candidates is None:
                move_domains = [[("company_id", "=", company_id), ("pnt_erp_id", ">", 0)]]
            else:
                move_ids = sorted({
                    source_id for model, source_id in candidates
                    if model in {"account.move", "account.invoice"}
                })
                move_domains = [
                    [
                        ("company_id", "=", company_id),
                        ("pnt_erp_id", "in", move_ids[offset:offset + PAGE_SIZE]),
                    ]
                    for offset in range(0, len(move_ids), PAGE_SIZE)
                ]
            for domain in move_domains:
                for move in self.client.search_read_pages("account.move", domain, wanted):
                    source_id = int(move["pnt_erp_id"])
                    model = "account.move" if move.get("move_type") == "entry" else "account.invoice"
                    if candidates is not None and (model, source_id) not in candidates:
                        continue
                    result.setdefault((model, source_id), []).append(
                        {"target_id": int(move["id"]), "date": move.get("date"), "label": move.get("name"), "posted": move.get("state") == "posted"}
                    )
        statement_fields = self.client.fields("account.bank.statement.line")
        if {"pnt_source_model", "pnt_erp_id"}.issubset(statement_fields):
            wanted = [name for name in ("id", "pnt_source_model", "pnt_erp_id", "date", "move_id") if name in statement_fields]
            if candidates is None:
                statement_domains = [[("company_id", "=", company_id), ("pnt_erp_id", ">", 0)]]
            else:
                statement_domains = []
                statement_models = sorted({
                    model for model, _source_id in candidates
                    if model not in {"account.move", "account.invoice", "payment.order"}
                })
                for model in statement_models:
                    statement_ids = sorted(source_id for source_model, source_id in candidates if source_model == model)
                    for offset in range(0, len(statement_ids), PAGE_SIZE):
                        statement_domains.append([
                            ("company_id", "=", company_id),
                            ("pnt_source_model", "=", model),
                            ("pnt_erp_id", "in", statement_ids[offset:offset + PAGE_SIZE]),
                        ])
            statements = [
                statement
                for domain in statement_domains
                for statement in self.client.search_read_pages(
                    "account.bank.statement.line", domain, wanted
                )
            ]
            statement_move_ids = sorted({_relation_id(item.get("move_id")) for item in statements} - {None})
            posted_moves: set[int] = set()
            if statement_move_ids:
                statement_moves = self.client.read("account.move", statement_move_ids, ["id", "state"])
                posted_moves = {int(move["id"]) for move in statement_moves if move.get("state") == "posted"}
            for statement in statements:
                if statement.get("pnt_source_model") and statement.get("pnt_erp_id"):
                    key = (str(statement["pnt_source_model"]), int(statement["pnt_erp_id"]))
                    result.setdefault(key, []).append(
                        {
                            "target_id": int(statement["id"]),
                            "date": statement.get("date"),
                            "label": "Bank statement line",
                            "posted": _relation_id(statement.get("move_id")) in posted_moves,
                        }
                    )
        batch_fields = self.client.fields("account.batch.payment")
        if "pnt_erp_id" in batch_fields:
            wanted = [name for name in ("id", "pnt_erp_id", "date", "name", "payment_ids") if name in batch_fields]
            if candidates is None:
                batch_domains = [[("company_id", "=", company_id), ("pnt_erp_id", ">", 0)]]
            else:
                batch_ids = sorted(source_id for model, source_id in candidates if model == "payment.order")
                batch_domains = [
                    [
                        ("company_id", "=", company_id),
                        ("pnt_erp_id", "in", batch_ids[offset:offset + PAGE_SIZE]),
                    ]
                    for offset in range(0, len(batch_ids), PAGE_SIZE)
                ]
            for domain in batch_domains:
                for batch in self.client.search_read_pages("account.batch.payment", domain, wanted):
                    key = ("payment.order", int(batch["pnt_erp_id"]))
                    payment_ids = batch.get("payment_ids") or []
                    payment_moves: list[int] = []
                    if payment_ids:
                        payment_fields = self.client.fields("account.payment")
                        if "move_id" in payment_fields:
                            payment_rows = self.client.read("account.payment", payment_ids, ["id", "move_id"])
                            payment_moves = [
                                move_id for payment in payment_rows
                                if (move_id := _relation_id(payment.get("move_id")))
                            ]
                    posted = False
                    if payment_moves:
                        payment_move_rows = self.client.read("account.move", payment_moves, ["id", "state"])
                        posted = len(payment_moves) == len(payment_ids) and all(
                            move.get("state") == "posted" for move in payment_move_rows
                        )
                    result.setdefault(key, []).append(
                        {"target_id": int(batch["id"]), "date": batch.get("date"), "label": batch.get("name"), "posted": posted}
                    )
        return result


def make_reader(config: dict[str, Any]) -> ReadOnlyERP:
    if config.get("protocol") in {"openerp5", "erppeek"}:
        return OpenERPReader(ErpPeekReaderClient(config))
    return OdooReader(OdooXMLRPCReader(config))
