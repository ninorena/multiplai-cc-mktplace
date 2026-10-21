"""get_token.py — which scopes the consent flow requests.

The real flow opens a browser, so InstalledAppFlow is replaced with a stub that
records the scopes it was built with and returns a fake credential.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_GET_TOKEN = (Path(__file__).resolve().parent.parent
              / "skills" / "gmail" / "scripts" / "get_token.py")


@pytest.fixture
def get_token():
    spec = importlib.util.spec_from_file_location("get_token", _GET_TOKEN)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def flow_stub(monkeypatch):
    calls = {}

    class _Creds:
        refresh_token = "rt"
        token_uri = "https://oauth2.googleapis.com/token"
        scopes = None

    class _Flow:
        client_config = {"client_id": "cid", "client_secret": "csec"}

        @classmethod
        def from_client_secrets_file(cls, path, scopes):
            calls["scopes"] = list(scopes)
            return cls()

        def run_local_server(self, **_kw):
            creds = _Creds()
            creds.scopes = calls["scopes"]
            return creds

    flow_mod = types.ModuleType("google_auth_oauthlib.flow")
    flow_mod.InstalledAppFlow = _Flow
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib", types.ModuleType("google_auth_oauthlib"))
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib.flow", flow_mod)
    return calls


def _run(get_token, monkeypatch, tmp_path, *extra):
    secret = tmp_path / "client_secret.json"
    secret.write_text("{}")
    monkeypatch.setattr(sys, "argv", ["get_token.py", str(secret), *extra])
    return get_token.main()


def test_default_requests_compose_and_readonly(get_token, flow_stub, monkeypatch, tmp_path, capsys):
    assert _run(get_token, monkeypatch, tmp_path) == 0
    assert set(flow_stub["scopes"]) == {get_token.SCOPE_COMPOSE, get_token.SCOPE_READONLY}
    assert "draft a reply" in capsys.readouterr().out


def test_readonly_requests_readonly_alone(get_token, flow_stub, monkeypatch, tmp_path, capsys):
    assert _run(get_token, monkeypatch, tmp_path, "--readonly") == 0
    assert flow_stub["scopes"] == [get_token.SCOPE_READONLY]
    out = capsys.readouterr().out
    assert "GMAIL_REFRESH_TOKEN=rt" in out
    assert "search your inbox" in out
