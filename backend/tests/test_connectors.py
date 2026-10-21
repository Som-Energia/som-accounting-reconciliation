import pytest
from datetime import date
import sys
from types import ModuleType
from types import SimpleNamespace

import app.connectors as connectors
from app.connectors import (
    ERPAuthenticationError,
    ErpPeekReaderClient,
    OdooReader,
    OdooXMLRPCReader,
    OpenERPReader,
    PAGE_SIZE,
)


class FakeErpPeekClient:
    def __init__(self, server, db, user, password, *, transport):
        self.connection = (server, db, user, password)
        self.transport = transport
        self.calls = []

    def fields(self, model):
        self.calls.append((model, "fields"))
        return {"id": {}, "name": {}}

    def search(self, model, domain, **kwargs):
        self.calls.append((model, "search", domain, kwargs))
        return [1]

    def read(self, model, ids, fields, *, context=None):
        self.calls.append((model, "read", ids, fields))
        return [{"id": 1, "name": "Test"}]

    def execute(self, model, method, *args, **kwargs):
        self.calls.append((model, method, args, kwargs))
        if method == "read_group":
            return []
        raise AssertionError(f"Unexpected erppeek method: {method}")


class FakeErpPeekFactory:
    def __init__(self):
        self.client = None

    def __call__(self, *args, **kwargs):
        self.client = FakeErpPeekClient(*args, **kwargs)
        return self.client


class FakeCommon:
    def authenticate(self, database, username, password, context):
        return 8


class FakeOdooObjectProxy:
    def __init__(self):
        self.calls = []

    def execute_kw(self, database, uid, password, model, method, args, kwargs):
        self.calls.append(method)
        if method == "fields_get":
            return {"name": {}}
        if method in {"search", "read", "read_group"}:
            return []
        raise AssertionError(f"Unexpected Odoo RPC method: {method}")


class OdooProxyFactory:
    def __init__(self):
        self.object = FakeOdooObjectProxy()
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        return FakeCommon() if url.endswith("common") else self.object


def test_openerp_connector_uses_erppeek_and_only_read_methods(monkeypatch):
    monkeypatch.setenv("OPENERP_SECRET_FILE", "/secret/openerp")
    monkeypatch.setattr("app.connectors.Path.read_text", lambda *_args, **_kwargs: "read-only-secret")
    factory = FakeErpPeekFactory()
    reader = ErpPeekReaderClient(
        {
            "url": "https://openerp.example.test",
            "database": "erp_db",
            "username": "reader",
            "password_file_env": "OPENERP_SECRET_FILE",
        },
        client_factory=factory,
    )

    assert factory.client.connection == (
        "https://openerp.example.test", "erp_db", "reader", "read-only-secret"
    )
    assert reader.fields("account.move.line") == {"id", "name"}
    assert reader.search("account.move.line", [("date", ">=", "2026-01-01")]) == [1]
    assert reader.read("account.move.line", [1], ["id", "name"]) == [{"id": 1, "name": "Test"}]
    assert set(call[1] for call in factory.client.calls) <= {"fields", "search", "read"}


def test_openerp_invalid_credentials_raise_provider_specific_error(monkeypatch):
    class FakeErpPeekError(Exception):
        pass

    def reject_login(*_args, **_kwargs):
        raise FakeErpPeekError("Invalid username or password")

    fake_erppeek = ModuleType("erppeek")
    fake_erppeek.Client = reject_login
    fake_erppeek.Error = FakeErpPeekError
    monkeypatch.setitem(sys.modules, "erppeek", fake_erppeek)
    monkeypatch.setenv("OPENERP_SECRET_FILE", "/secret/openerp")
    monkeypatch.setattr("app.connectors.Path.read_text", lambda *_args, **_kwargs: "secret-value")

    with pytest.raises(ERPAuthenticationError) as error:
        ErpPeekReaderClient(
            {
                "url": "https://openerp.example.test/xmlrpc?token=url-secret",
                "database": "erp_db",
                "username": "reader",
                "password_file_env": "OPENERP_SECRET_FILE",
            }
        )

    assert error.value.system == "OpenERP"
    assert error.value.endpoint == "https://openerp.example.test"
    assert error.value.database == "erp_db"
    assert error.value.username == "reader"
    assert "secret-value" not in str(error.value)
    assert "url-secret" not in str(error.value)


def test_odoo_connector_keeps_its_external_xmlrpc_endpoint(monkeypatch):
    monkeypatch.setenv("ODOO_SECRET", "test-only-secret")
    factory = OdooProxyFactory()
    reader = OdooXMLRPCReader(
        {
            "url": "https://odoo.example.test",
            "database": "odoo_db",
            "username": "reader",
            "password_env": "ODOO_SECRET",
            "protocol": "odoo18",
        },
        proxy_factory=factory,
    )
    reader.fields("account.move.line")
    assert factory.urls == [
        "https://odoo.example.test/xmlrpc/2/common",
        "https://odoo.example.test/xmlrpc/2/object",
    ]
    assert set(factory.object.calls) == {"fields_get"}


def test_odoo_invalid_credentials_raise_provider_specific_error(monkeypatch):
    class RejectingCommon:
        def authenticate(self, _database, _username, _password, _context):
            return False

    class RejectingProxyFactory:
        def __call__(self, url):
            return RejectingCommon() if url.endswith("common") else FakeOdooObjectProxy()

    monkeypatch.setenv("ODOO_SECRET", "secret-value")

    with pytest.raises(ERPAuthenticationError) as error:
        OdooXMLRPCReader(
            {
                "url": "https://odoo.example.test/rpc?token=url-secret",
                "database": "odoo_db",
                "username": "reader",
                "password_env": "ODOO_SECRET",
                "protocol": "odoo18",
            },
            proxy_factory=RejectingProxyFactory(),
        )

    assert error.value.system == "Odoo"
    assert error.value.endpoint == "https://odoo.example.test"
    assert error.value.database == "odoo_db"
    assert error.value.username == "reader"
    assert "secret-value" not in str(error.value)
    assert "url-secret" not in str(error.value)


def test_connector_fails_closed_when_secret_is_missing(monkeypatch):
    monkeypatch.delenv("MISSING_ERP_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="MISSING_ERP_PASSWORD"):
        ErpPeekReaderClient(
            {
                "url": "https://erp.example.test",
                "database": "test_db",
                "username": "reader",
                "password_env": "MISSING_ERP_PASSWORD",
            },
            client_factory=FakeErpPeekFactory(),
        )


def test_make_reader_accepts_the_erppeek_profile_protocol(monkeypatch):
    fake_client = FakeErpPeekReaderClient()
    monkeypatch.setattr(connectors, "ErpPeekReaderClient", lambda _config: fake_client)
    reader = connectors.make_reader({"protocol": "erppeek"})
    assert isinstance(reader, connectors.OpenERPReader)
    assert reader.client is fake_client


class FakeErpPeekReaderClient:
    """Marker client used to verify profile dispatch without network access."""


class FakeAccountReaderClient:
    def fields(self, _model):
        return {"id", "code", "name", "odoo_account_code"}

    def search(self, _model, _domain, **_kwargs):
        return [1, 2, 3]

    def read(self, _model, ids, _fields):
        return [
            {"id": 1, "code": "430000000001", "name": "Posting account", "odoo_account_code": "430000001"},
            {"id": 2, "code": "430", "name": "Parent account", "odoo_account_code": ""},
            {"id": 3, "code": "12345678901", "name": "Short account", "odoo_account_code": ""},
        ]

    def search_read_pages(self, model, domain, fields):
        yield from self.read(model, self.search(model, domain), fields)

    def search(self, _model, _domain):
        return [1, 2, 3]


class PagedAccountClient:
    def __init__(self, rows):
        self.rows = rows
        self.offsets = []

    def fields(self, _model):
        return set(self.rows[0])

    def search(self, _model, _domain, *, offset=0):
        self.offsets.append(offset)
        return [row["id"] for row in self.rows[offset:offset + PAGE_SIZE]]

    def read(self, _model, ids, field_names):
        id_set = set(ids)
        return [
            {field: row[field] for field in field_names if field in row}
            for row in self.rows if row["id"] in id_set
        ]

    def search_read_pages(self, model, domain, fields):
        offset = 0
        while True:
            ids = self.search(model, domain, offset=offset)
            if not ids:
                return
            yield from self.read(model, ids, fields)
            offset += len(ids)
            if len(ids) < PAGE_SIZE:
                return


def test_openerp_account_list_ignores_parent_codes_shorter_than_twelve_digits():
    accounts = OpenERPReader(FakeAccountReaderClient()).accounts(company_id=1)
    assert [account["code"] for account in accounts] == ["430000000001"]


def test_account_connectors_read_beyond_the_first_five_hundred_records():
    source_rows = [
        {"id": number, "code": str(number).zfill(12), "name": "Posting", "odoo_account_code": "100000000"}
        for number in range(1, PAGE_SIZE + 2)
    ]
    source_rows.append({"id": PAGE_SIZE + 2, "code": "1", "name": "Parent", "odoo_account_code": ""})
    source_client = PagedAccountClient(source_rows)
    source_accounts = OpenERPReader(source_client).accounts(company_id=1)
    assert len(source_accounts) == PAGE_SIZE + 1
    assert source_client.offsets == [0, PAGE_SIZE]

    target_rows = [
        {"id": number, "code": str(number).zfill(9), "code_store": str(number).zfill(9), "name": "Target"}
        for number in range(1, PAGE_SIZE + 2)
    ]
    target_client = PagedAccountClient(target_rows)
    target_accounts = OdooReader(target_client).accounts(company_id=1)
    assert len(target_accounts) == PAGE_SIZE + 1
    assert target_client.offsets == [0, PAGE_SIZE]


def test_open_erp_cumulative_timeout_does_not_block_period_comparison(monkeypatch):
    reader = OpenERPReader(SimpleNamespace(client=SimpleNamespace(major_version="5.0")))

    def timeout(*_args):
        raise TimeoutError

    monkeypatch.setattr(reader, "period_totals", timeout)
    assert reader.cumulative_totals(1, date(2026, 10, 14), "posted") is None
