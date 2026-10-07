"""Smoke tests for the web UI against a temp DB with a little data."""

import os

import pytest

from leadcore import db, pipeline
from leadcore.generate.candidates import Target, generate

from .conftest import ListLoader, rec


@pytest.fixture
def client(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'web.db'}"
    db.migrate(url)
    with db.session(url) as s:
        pipeline.ingest(s, loaders=[ListLoader([
            rec("jane.doe@acme.com", "Jane", row=2), rec("john.smith@acme.com", "John", row=3),
            rec("mary.jones@acme.com", "Mary", row=4), rec("hoon@short.com", "Hoon", row=5)])])
        run, _, _ = pipeline.run_inference(s, use_nicknames=True)
        generate(s, [Target("Robert", "Brown", "acme.com")], run.id)
    monkeypatch.setenv("LEADCORE_DB_URL", url)
    from fastapi.testclient import TestClient
    from leadcore.web.app import app
    return TestClient(app)


def test_pages_render(client):
    for path in ("/", "/firms", "/firms?tier=C-", "/generate", "/verify", "/validation"):
        r = client.get(path)
        assert r.status_code == 200, path


def test_firm_detail(client):
    r = client.get("/firms/acme.com")
    assert r.status_code == 200 and "acme.com" in r.text and "first.last" in r.text


def test_generate_post(client):
    r = client.post("/generate", data={"first": "Robert", "last": "Lang", "domain": "short.com"})
    assert r.status_code == 200
    assert "robert@short.com" in r.text and "robert.lang@short.com" in r.text  # C- fallback shown


def test_verify_export_download(client):
    r = client.get("/verify/export")
    assert r.status_code == 200 and r.text.startswith("email,domain,")


def test_verify_import_roundtrip(client):
    csv = "email,status\nrobert.brown@acme.com,valid\n"
    r = client.post("/verify/import", files={"file": ("zb.csv", csv, "text/csv")},
                    data={"service": "generic"}, follow_redirects=False)
    assert r.status_code == 303
    r = client.get("/verify")
    assert "deliverable" in r.text
