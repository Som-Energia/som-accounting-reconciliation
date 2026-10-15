import pytest

import app.connectors as connectors
from app.connectors import ErpPeekReaderClient, OdooXMLRPCReader


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

    def read(self, model, ids, fields):
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
