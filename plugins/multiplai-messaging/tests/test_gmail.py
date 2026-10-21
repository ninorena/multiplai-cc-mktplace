"""Unit tests for the gmail skill engine (gmail.py).

Covers Fix 8 (_extract_body single-DFS, text/plain preferred) and Fix 6 (reply
drafts set the recipient explicitly). No network: a fake Gmail service stands in
for the discovery client.

Also locks the three guarantees the skill's safety claim rests on (MSG-1):
scope containment (``_assert_scopes``), the INBOX read/reply gate, and the
structural absence of any send path in the source. These are tripwires — if a
future refactor weakens a guard, the matching test goes red instead of the
weakness shipping silently.
"""
from __future__ import annotations

import base64
import email
import json
from pathlib import Path

import pytest


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("utf-8")


def _text_part(mime: str, text: str) -> dict:
    return {"mimeType": mime, "body": {"data": _b64(text)}}


def _multipart(mime: str, *parts: dict) -> dict:
    return {"mimeType": mime, "parts": list(parts)}


class _Exec:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _Messages:
    def __init__(self, get_result):
        self._get_result = get_result

    def get(self, **_kw):
        return _Exec(self._get_result)


class _Drafts:
    def __init__(self, sink):
        self._sink = sink

    def create(self, userId, body):  # noqa: N803 - matches google client kwarg
        self._sink["body"] = body
        return _Exec({"id": "draft-xyz"})


class _Users:
    def __init__(self, get_result, sink):
        self._messages = _Messages(get_result)
        self._drafts = _Drafts(sink)

    def messages(self):
        return self._messages

    def drafts(self):
        return self._drafts


class FakeService:
    def __init__(self, get_result=None, sink=None):
        self._users = _Users(get_result, sink if sink is not None else {})

    def users(self):
        return self._users


def _decode_created_to(sink: dict) -> str:
    raw = sink["body"]["message"]["raw"]
    msg = email.message_from_bytes(base64.urlsafe_b64decode(raw.encode("utf-8")))
    return msg["To"] or ""


# --------------------------------------------------------------------------- #
# Fix 8 — _extract_body
# --------------------------------------------------------------------------- #
class TestExtractBody:
    def test_flat_plain(self, gmail):
        payload = _text_part("text/plain", "hello plain")
        assert gmail._extract_body(payload) == "hello plain"

    def test_flat_html_only(self, gmail):
        payload = _text_part("text/html", "<p>hi html</p>")
        assert gmail._extract_body(payload) == "<p>hi html</p>"

    def test_prefers_plain_over_html_simple_alternative(self, gmail):
        payload = _multipart(
            "multipart/alternative",
            _text_part("text/html", "<p>HTML</p>"),
            _text_part("text/plain", "PLAIN"),
        )
        assert gmail._extract_body(payload) == "PLAIN"

    def test_html_sibling_before_nested_plain(self, gmail):
        # The confirmed failure case: a top-level text/html sibling appears
        # BEFORE a multipart/alternative that contains the text/plain body.
        # The plain body must still win.
        payload = _multipart(
            "multipart/mixed",
            _text_part("text/html", "<p>outer html</p>"),
            _multipart(
                "multipart/alternative",
                _text_part("text/html", "<p>inner html</p>"),
                _text_part("text/plain", "the real plain body"),
            ),
        )
        assert gmail._extract_body(payload) == "the real plain body"

    def test_deeply_nested_plain(self, gmail):
        payload = _multipart(
            "multipart/mixed",
            _multipart(
                "multipart/related",
                _multipart(
                    "multipart/alternative",
                    _text_part("text/plain", "deep plain"),
                    _text_part("text/html", "<p>deep html</p>"),
                ),
            ),
        )
        assert gmail._extract_body(payload) == "deep plain"

    def test_html_fallback_when_no_plain(self, gmail):
        payload = _multipart(
            "multipart/mixed",
            _text_part("text/html", "<p>only html</p>"),
        )
        assert gmail._extract_body(payload) == "<p>only html</p>"

    def test_empty_payload(self, gmail):
        assert gmail._extract_body({"mimeType": "multipart/mixed", "parts": []}) == ""

    def test_ignores_non_text_parts(self, gmail):
        payload = _multipart(
            "multipart/mixed",
            {"mimeType": "image/png", "body": {"attachmentId": "a1"}},
            _text_part("text/plain", "body after image"),
        )
        assert gmail._extract_body(payload) == "body after image"


# --------------------------------------------------------------------------- #
# Fix 6 — reply recipient resolution
# --------------------------------------------------------------------------- #
def _orig_msg(headers: list[tuple[str, str]], thread="T1") -> dict:
    return {
        "threadId": thread,
        "labelIds": ["INBOX"],
        "payload": {"headers": [{"name": n, "value": v} for n, v in headers]},
    }


class TestReplyRecipient:
    def _payload_file(self, tmp_path, payload: dict):
        p = tmp_path / "payload.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        return str(p)

    def test_reply_without_to_uses_from_header(self, gmail, tmp_path, capsys):
        sink: dict = {}
        orig = _orig_msg([
            ("Message-ID", "<abc@x>"),
            ("Subject", "Contract"),
            ("From", "Marco Rossi <marco@example.com>"),
        ])
        svc = FakeService(get_result=orig, sink=sink)
        inp = self._payload_file(tmp_path, {"body": "Thanks Marco"})
        gmail.cmd_draft(svc, reply_to="orig-id", input_file=inp)
        assert _decode_created_to(sink) == "Marco Rossi <marco@example.com>"
        out = capsys.readouterr().out
        assert "To: Marco Rossi <marco@example.com>" in out
        assert "inherited from thread" not in out

    def test_reply_prefers_reply_to_over_from(self, gmail, tmp_path):
        sink: dict = {}
        orig = _orig_msg([
            ("Message-ID", "<abc@x>"),
            ("From", "Marco <marco@example.com>"),
            ("Reply-To", "list@example.com"),
        ])
        svc = FakeService(get_result=orig, sink=sink)
        inp = self._payload_file(tmp_path, {"body": "hi"})
        gmail.cmd_draft(svc, reply_to="orig-id", input_file=inp)
        assert _decode_created_to(sink) == "list@example.com"

    def test_explicit_to_overrides_headers(self, gmail, tmp_path):
        sink: dict = {}
        orig = _orig_msg([("Message-ID", "<abc@x>"), ("From", "marco@example.com")])
        svc = FakeService(get_result=orig, sink=sink)
        inp = self._payload_file(tmp_path, {"to": "someone@else.com", "body": "hi"})
        gmail.cmd_draft(svc, reply_to="orig-id", input_file=inp)
        assert _decode_created_to(sink) == "someone@else.com"

    def test_reply_with_no_recipient_headers_dies(self, gmail, tmp_path):
        sink: dict = {}
        orig = _orig_msg([("Message-ID", "<abc@x>"), ("Subject", "no sender")])
        svc = FakeService(get_result=orig, sink=sink)
        inp = self._payload_file(tmp_path, {"body": "hi"})
        with pytest.raises(SystemExit):
            gmail.cmd_draft(svc, reply_to="orig-id", input_file=inp)
        assert "body" not in sink  # never reached drafts.create

    def test_new_draft_without_to_dies(self, gmail, tmp_path):
        svc = FakeService(sink={})
        inp = self._payload_file(tmp_path, {"body": "hi"})
        with pytest.raises(SystemExit):
            gmail.cmd_draft(svc, reply_to="none", input_file=inp)

    def test_draft_logs_audit_event(self, gmail, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(gmail, "log_event", lambda *a, **k: calls.append((a, k)))
        sink: dict = {}
        orig = _orig_msg([("Message-ID", "<abc@x>"), ("From", "marco@example.com")])
        svc = FakeService(get_result=orig, sink=sink)
        inp = tmp_path / "p.json"
        inp.write_text(json.dumps({"body": "hi"}), encoding="utf-8")
        gmail.cmd_draft(svc, reply_to="orig-id", input_file=str(inp))
        assert calls, "expected a draft audit log_event"
        (args, kwargs) = calls[0]
        assert args[0] == "gmail" and args[1] == "draft"
        assert kwargs.get("mode") == "reply"
        assert kwargs.get("draft_id") == "draft-xyz"


# --------------------------------------------------------------------------- #
# MSG-1a — scope containment (_assert_scopes)
# --------------------------------------------------------------------------- #
class _Creds:
    """Minimal stand-in for google credentials — _assert_scopes only reads
    ``.token`` to build the tokeninfo URL."""

    token = "fake-access-token"


class _FakeResp:
    """Context-manager stand-in for urllib.request.urlopen; json.load reads
    ``.read()``."""

    def __init__(self, raw: bytes):
        self._raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def read(self):
        return self._raw


def _patch_tokeninfo(monkeypatch, gmail, *, scope=None, raw=None, raise_exc=None):
    """Point gmail's tokeninfo HTTP call at a fake response.

    ``scope`` → a well-formed ``{"scope": "..."}`` JSON body; ``raw`` → arbitrary
    bytes (e.g. malformed JSON); ``raise_exc`` → urlopen itself blows up
    (unreachable network).
    """
    if raw is None and scope is not None:
        raw = json.dumps({"scope": scope}).encode("utf-8")

    def fake_urlopen(url, timeout=None):  # noqa: ARG001
        if raise_exc is not None:
            raise raise_exc
        return _FakeResp(raw if raw is not None else b"{}")

    monkeypatch.setattr(gmail.urllib.request, "urlopen", fake_urlopen)


class TestAssertScopes:
    def test_exact_allowed_scopes_proceed(self, gmail, monkeypatch):
        allowed = f"{gmail.SCOPE_COMPOSE} {gmail.SCOPE_READONLY}"
        _patch_tokeninfo(monkeypatch, gmail, scope=allowed)
        # No SystemExit — the guard lets the exact allowed set through.
        assert gmail._assert_scopes(_Creds()) is None

    def test_only_compose_proceeds(self, gmail, monkeypatch):
        # A subset of the allowed set is fine (extra = granted - allowed = ∅).
        _patch_tokeninfo(monkeypatch, gmail, scope=gmail.SCOPE_COMPOSE)
        assert gmail._assert_scopes(_Creds()) is None

    def test_only_readonly_proceeds(self, gmail, monkeypatch):
        # The credential get_token.py --readonly mints.
        _patch_tokeninfo(monkeypatch, gmail, scope=gmail.SCOPE_READONLY)
        assert gmail._assert_scopes(_Creds()) is None

    def test_extra_send_scope_aborts(self, gmail, monkeypatch):
        broad = (f"{gmail.SCOPE_COMPOSE} {gmail.SCOPE_READONLY} "
                 "https://www.googleapis.com/auth/gmail.send")
        _patch_tokeninfo(monkeypatch, gmail, scope=broad)
        with pytest.raises(SystemExit):
            gmail._assert_scopes(_Creds())

    def test_extra_modify_scope_aborts(self, gmail, monkeypatch):
        broad = (f"{gmail.SCOPE_READONLY} "
                 "https://www.googleapis.com/auth/gmail.modify")
        _patch_tokeninfo(monkeypatch, gmail, scope=broad)
        with pytest.raises(SystemExit):
            gmail._assert_scopes(_Creds())

    def test_empty_scope_fails_closed(self, gmail, monkeypatch):
        _patch_tokeninfo(monkeypatch, gmail, scope="")
        with pytest.raises(SystemExit):
            gmail._assert_scopes(_Creds())

    def test_tokeninfo_unreachable_fails_closed(self, gmail, monkeypatch):
        _patch_tokeninfo(monkeypatch, gmail, raise_exc=OSError("connection refused"))
        with pytest.raises(SystemExit):
            gmail._assert_scopes(_Creds())

    def test_tokeninfo_malformed_fails_closed(self, gmail, monkeypatch):
        _patch_tokeninfo(monkeypatch, gmail, raw=b"<html>not json</html>")
        with pytest.raises(SystemExit):
            gmail._assert_scopes(_Creds())


# --------------------------------------------------------------------------- #
# MSG-1b — INBOX gate (read + reply-draft)
# --------------------------------------------------------------------------- #
def _msg(labels, headers=(("From", "marco@example.com"),)) -> dict:
    """A fetched message with the given labelIds and a minimal text/plain body."""
    return {
        "threadId": "T1",
        "labelIds": list(labels),
        "payload": {
            "mimeType": "text/plain",
            "body": {"data": _b64("hello body")},
            "headers": [{"name": n, "value": v} for n, v in headers],
        },
    }


class TestReadInboxGate:
    def test_inbox_message_is_read(self, gmail, capsys):
        svc = FakeService(get_result=_msg(["INBOX", "IMPORTANT"]))
        gmail.cmd_read(svc, "m1")
        out = capsys.readouterr().out
        assert "hello body" in out  # reached the body — no refusal

    def test_non_inbox_message_refused(self, gmail):
        # e.g. a SENT / archived message that is not in the inbox.
        svc = FakeService(get_result=_msg(["SENT"]))
        with pytest.raises(SystemExit):
            gmail.cmd_read(svc, "m1")

    def test_missing_labels_fails_closed(self, gmail):
        msg = _msg(["INBOX"])
        del msg["labelIds"]  # no labelIds at all → treated as not-INBOX
        svc = FakeService(get_result=msg)
        with pytest.raises(SystemExit):
            gmail.cmd_read(svc, "m1")

    def test_empty_labels_fails_closed(self, gmail):
        svc = FakeService(get_result=_msg([]))
        with pytest.raises(SystemExit):
            gmail.cmd_read(svc, "m1")


class TestReplyInboxGate:
    def _payload_file(self, tmp_path, payload: dict):
        p = tmp_path / "payload.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        return str(p)

    def test_reply_to_inbox_message_drafts(self, gmail, tmp_path):
        sink: dict = {}
        svc = FakeService(get_result=_msg(["INBOX"]), sink=sink)
        inp = self._payload_file(tmp_path, {"body": "thanks"})
        gmail.cmd_draft(svc, reply_to="orig-id", input_file=inp)
        assert "body" in sink  # a draft was created

    def test_reply_to_non_inbox_refused_before_write(self, gmail, tmp_path):
        sink: dict = {}
        svc = FakeService(get_result=_msg(["SENT"]), sink=sink)
        inp = self._payload_file(tmp_path, {"body": "thanks"})
        with pytest.raises(SystemExit):
            gmail.cmd_draft(svc, reply_to="orig-id", input_file=inp)
        assert "body" not in sink  # never reached drafts.create

    def test_reply_missing_labels_fails_closed_before_write(self, gmail, tmp_path):
        msg = _msg(["INBOX"])
        del msg["labelIds"]
        sink: dict = {}
        svc = FakeService(get_result=msg, sink=sink)
        inp = self._payload_file(tmp_path, {"body": "thanks"})
        with pytest.raises(SystemExit):
            gmail.cmd_draft(svc, reply_to="orig-id", input_file=inp)
        assert "body" not in sink

    def test_reply_empty_labels_fails_closed_before_write(self, gmail, tmp_path):
        sink: dict = {}
        svc = FakeService(get_result=_msg([]), sink=sink)
        inp = self._payload_file(tmp_path, {"body": "thanks"})
        with pytest.raises(SystemExit):
            gmail.cmd_draft(svc, reply_to="orig-id", input_file=inp)
        assert "body" not in sink


# --------------------------------------------------------------------------- #
# MSG-1c — send-absence source tripwire
# --------------------------------------------------------------------------- #
class TestNoSendPath:
    """Crude but exact: the review verified by grep that the only write call is
    ``users.drafts().create`` — no ``messages.send`` / ``drafts.send`` anywhere.
    Encode that as a source-level assertion so the tripwire fires the moment
    someone adds a send path."""

    def _source(self, gmail) -> str:
        return Path(gmail.__file__).read_text(encoding="utf-8")

    def test_no_send_api_calls_in_source(self, gmail):
        src = self._source(gmail)
        # Only paren'd call forms — the module docstring documents "No
        # ``messages.send``, no ``drafts.send``" as prose (no trailing "("),
        # so it must not trip the guard. An actual send is `.send(` and thus
        # always matches the catch-all below; the two specific forms make the
        # failure message point straight at the offending API.
        forbidden = [
            "messages().send(",
            "drafts().send(",
            ".send(",
        ]
        hits = [tok for tok in forbidden if tok in src]
        assert not hits, f"forbidden send API call(s) present in gmail.py: {hits}"

    def test_draft_create_still_present(self, gmail):
        # Positive control: the one legitimate write must exist, so the test above
        # is guarding a real capability rather than an empty file.
        assert "drafts().create" in self._source(gmail)
