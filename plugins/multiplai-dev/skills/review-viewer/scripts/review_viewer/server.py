"""The HTTP server between the page and the Claude Code session.

It never calls a model. It serves the page, answers read-only questions about
the review, and carries messages: questions and decisions from the page go to
`inbox.jsonl`, which the session watches; answers the session writes to
`outbox.jsonl` go back to the page through `/api/poll`.

Every `/api/*` request needs the per-start token in `X-Review-Token`. The token
is written only to `server.token` and `open.html` (both 0600, deleted on exit)
and never printed or logged: the session's stdout is its transcript.
"""

from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from multiplai_core.log_utils import log_event
from pydantic import ValidationError

from . import netinfo, registry
from .gitdata import GitError, PathNotInReview, allowed_paths, file_view
from .mailbox import Mailbox, new_question_id, utc_now, write_private
from .models import Anchor, FindingsFile, InboxRow, Walkthrough, findings_digest
from .walkthrough import Served, served_path, walkthrough_path

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
COMPONENT = "review-viewer"
MAX_QUESTION_CHARS = 8000
MAX_BODY_BYTES = 64 * 1024
STEP_ID_RE = re.compile(r"^[a-z0-9-]{1,40}$")
REJECT_LOG_INTERVAL = 60.0
STOP_MESSAGES = {
    "stop": "viewer stopped by stop --box",
    "findings_changed": "viewer restarted because a findings file changed",
}

CSP = ("default-src 'none'; script-src 'self' https://cdnjs.cloudflare.com; "
       "style-src 'self' https://cdnjs.cloudflare.com; img-src 'self' data:; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class ViewerHTTPServer(ThreadingHTTPServer):
    """Refuses a port that is already in use.

    `ThreadingHTTPServer` sets SO_REUSEADDR, and on Windows that flag lets a
    second process bind the *same* port: two servers then split requests at
    random, and one review's questions land in another's mailbox. Here the
    bind must fail so the search moves on to the next port.
    """
    allow_reuse_address = False
    daemon_threads = True


@dataclass
class TargetState:
    findings: FindingsFile
    mailbox: Mailbox
    allowed: set[str]
    pr: dict | None = None
    notice: str | None = None
    stats: dict | None = None

    @property
    def slug(self) -> str:
        return self.findings.target.slug

    def walkthrough(self) -> Walkthrough | None:
        """The walkthrough `walkthrough put` last wrote, or None. It was
        checked when it was put; a file that no longer parses is treated as
        absent rather than served half-read."""
        path = walkthrough_path(self.mailbox.dir)
        try:
            return Walkthrough.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            log.warning("walkthrough for %s does not parse: %s", self.slug, exc)
            return None


@dataclass
class Viewer:
    targets: dict[str, TargetState]
    token: str
    agent: str
    session_id: str
    idle_minutes: float
    started: str = field(default_factory=utc_now)
    last_seen: float = field(default_factory=time.monotonic)
    httpd: ViewerHTTPServer | None = None
    stop_reason: str | None = None
    stopped: threading.Event = field(default_factory=threading.Event)
    _rejects: dict[int, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def port(self) -> int:
        assert self.httpd is not None
        return self.httpd.server_address[1]

    def whoami(self) -> dict:
        return {"agent": self.agent, "session_id": self.session_id, "pid": os.getpid(),
                "started": self.started, "targets": list(self.targets),
                "mailboxes": [str(t.mailbox.dir.resolve()) for t in self.targets.values()],
                "digests": {str(t.mailbox.dir.resolve()): findings_digest(t.findings)
                            for t in self.targets.values()}}

    def note_reject(self, status: int, route: str, reason: str) -> None:
        """Log a refused request, at most once a minute per status code, so a
        scanner hitting the port cannot flood activity.log."""
        now = time.monotonic()
        with self._lock:
            last = self._rejects.get(status)
            if last is not None and now - last < REJECT_LOG_INTERVAL:
                return
            self._rejects[status] = now
        log.warning("refused %s %s: %s", status, route, reason)
        log_event(COMPONENT, "rejected_request", f"refused request: {reason}",
                  session_id=self.session_id, level="WARNING", status=status, route=route)


def summarise(state: TargetState) -> dict:
    counts: dict[str, dict[str, int]] = {"severity": {}, "status": {}}
    for f in state.findings.findings:
        counts["severity"][f.severity] = counts["severity"].get(f.severity, 0) + 1
        counts["status"][f.status] = counts["status"].get(f.status, 0) + 1
    t = state.findings.target
    return {"slug": t.slug, "label": t.label, "counts": counts}


class _Reject(Exception):
    def __init__(self, status: int, error: str, log_reason: str | None = None):
        super().__init__(error)
        self.status, self.error, self.log_reason = status, error, log_reason


def make_handler(viewer: Viewer):
    class Handler(BaseHTTPRequestHandler):
        server_version = "review-viewer"
        sys_version = ""

        def log_message(self, fmt, *args):  # stdlib access log → DEBUG only
            log.debug("%s " + fmt, self.client_address[0], *args)

        # --- responses --------------------------------------------------------

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", CSP)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, status: int = 200) -> None:
            self._send(status, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                       "application/json; charset=utf-8")

        # --- checks -----------------------------------------------------------

        def _check_origin(self) -> None:
            origin = self.headers.get("Origin")
            if origin is None:
                return
            host = self.headers.get("Host", "")
            if urlparse(origin).netloc.lower() != host.lower():
                raise _Reject(403, "forbidden", "foreign origin")

        def _check_token(self) -> None:
            given = self.headers.get("X-Review-Token", "")
            if not hmac.compare_digest(given.encode("utf-8"), viewer.token.encode("utf-8")):
                raise _Reject(401, "unauthorized", "bad token")

        def _body(self) -> dict:
            ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if ctype != "application/json":
                raise _Reject(415, "content type must be application/json", "wrong content type")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise _Reject(400, "bad content length")
            if length < 0:
                raise _Reject(400, "bad content length")
            if length > MAX_BODY_BYTES:
                raise _Reject(413, "body too large")
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise _Reject(400, "invalid json")
            if not isinstance(data, dict):
                raise _Reject(400, "expected a json object")
            return data

        def _target(self, slug: str | None) -> TargetState:
            state = viewer.targets.get(slug or "")
            if state is None:
                raise _Reject(404, "unknown target")
            return state

        # --- dispatch ---------------------------------------------------------

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            url = urlparse(self.path)
            route = url.path
            try:
                self._check_origin()
                if route.startswith("/api/"):
                    self._check_token()
                    # whoami is how `list`, `serve` and `stop` probe the server;
                    # counting it as activity would keep a server with no open
                    # page alive for as long as the session keeps checking.
                    if route != "/api/whoami":
                        viewer.last_seen = time.monotonic()
                    body = self._body() if method == "POST" else {}
                    return self._api(method, route, parse_qs(url.query), body)
                if method == "GET":
                    return self._static(route)
                raise _Reject(405, "method not allowed")
            except _Reject as rej:
                if rej.log_reason:
                    viewer.note_reject(rej.status, route, rej.log_reason)
                self._json({"error": rej.error}, rej.status)
            except Exception:
                log.error("unhandled error on %s %s", method, route, exc_info=True)
                try:
                    self._json({"error": "internal"}, 500)
                except OSError:
                    pass

        def _static(self, route: str) -> None:
            name = "index.html" if route in ("/", "/index.html") else None
            if route.startswith("/static/"):
                name = route[len("/static/"):]
            if not name or "/" in name or name.startswith("."):
                raise _Reject(404, "not found")
            path = STATIC_DIR / name
            if not path.is_file():
                raise _Reject(404, "not found")
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self._send(200, path.read_bytes(), ctype)

        def _api(self, method: str, route: str, query: dict, body: dict) -> None:
            q = {k: v[0] for k, v in query.items()}
            if method == "GET":
                if route == "/api/whoami":
                    return self._json(viewer.whoami())
                if route == "/api/alive":
                    return self._json({"ok": True})
                if route == "/api/targets":
                    return self._json({"targets": [summarise(s) for s in viewer.targets.values()]})
                if route == "/api/poll":
                    state = self._target(q.get("target"))
                    try:
                        since = int(q.get("since", "0"))
                    except ValueError:
                        since = 0
                    rows, n = state.mailbox.read_outbox(since)
                    return self._json({"answers": rows, "n": n})
                if route.startswith("/api/targets/"):
                    rest = route[len("/api/targets/"):]
                    if rest.endswith("/file"):
                        state = self._target(rest[: -len("/file")])
                        return self._file(state, q.get("path", ""))
                    if rest.endswith("/walkthrough"):
                        state = self._target(rest[: -len("/walkthrough")])
                        wt = state.walkthrough()
                        if wt is None:
                            raise _Reject(404, "no walkthrough yet")
                        return self._json(wt.model_dump(mode="json"))
                    state = self._target(rest)
                    return self._json({
                        "findings": state.findings.model_dump(mode="json"),
                        "files": state.findings.target.files_changed,
                        "decisions": state.mailbox.read_decisions(),
                        "questions": [r for r in state.mailbox.read_inbox()
                                      if r.get("kind") == "question"],
                        "pr": state.pr,
                        "notice": state.notice,
                        "stats": state.stats,
                    })
            if method == "POST":
                if route == "/api/ask":
                    return self._ask(body)
                if route == "/api/decision":
                    return self._decision(body)
                if route == "/api/shutdown":
                    reason = body.get("reason")
                    reason = reason if reason in STOP_MESSAGES else "stop"
                    viewer.stop_reason = reason
                    self._json({"ok": True})
                    log_event(COMPONENT, "stop", STOP_MESSAGES[reason],
                              session_id=viewer.session_id, reason=reason)
                    threading.Thread(target=viewer.httpd.shutdown, daemon=True).start()
                    return
            raise _Reject(404, "not found")

        def _file(self, state: TargetState, path: str) -> None:
            try:
                view = file_view(state.findings.target, path, state.findings, state.allowed)
            except PathNotInReview:
                raise _Reject(404, "path is not part of this review")
            except GitError as exc:
                log.warning("file view failed for %s: %s", path, exc)
                raise _Reject(422, "git could not read this file at the reviewed commits")
            self._json(view.to_dict())

        def _ask(self, body: dict) -> None:
            state = self._target(body.get("target"))
            text = body.get("text")
            if not isinstance(text, str) or not (1 <= len(text.strip()) and len(text) <= MAX_QUESTION_CHARS):
                raise _Reject(400, f"text must be 1-{MAX_QUESTION_CHARS} characters")
            finding_id = body.get("finding_id")
            if finding_id is not None and not any(f.id == finding_id for f in state.findings.findings):
                raise _Reject(404, "unknown finding")
            try:
                anchor = Anchor.model_validate(body["anchor"]) if body.get("anchor") else None
            except ValidationError:
                raise _Reject(400, "invalid anchor")
            step_id = body.get("step_id")
            if step_id is not None:
                if not isinstance(step_id, str) or not STEP_ID_RE.match(step_id):
                    raise _Reject(400, "invalid step_id")
                wt = state.walkthrough()
                if wt is None or not any(s.id == step_id for s in wt.steps):
                    raise _Reject(404, "unknown step")
            explain = body.get("explain") is True
            if explain and anchor is None:
                raise _Reject(400, "explain needs the block's anchor")
            row = InboxRow(id=new_question_id(), ts=utc_now(), target=state.slug,
                           kind="question", finding_id=finding_id, anchor=anchor, text=text,
                           step_id=step_id, explain=explain)
            state.mailbox.append_inbox(row)
            about = f"block {anchor.path}:{anchor.line_start}" if explain else f"finding {finding_id}" if finding_id else (
                f"step {step_id}" if step_id else (
                    f"{anchor.path}:{anchor.line_start}" if anchor else "the review"))
            log_event(COMPONENT, "question", f"question {row.id} on {about}",
                      session_id=viewer.session_id, target=state.slug,
                      finding_id=finding_id, chars=len(text))
            self._json({"id": row.id})

        def _decision(self, body: dict) -> None:
            state = self._target(body.get("target"))
            finding_id = body.get("finding_id")
            if not any(f.id == finding_id for f in state.findings.findings):
                raise _Reject(404, "unknown finding")
            decision = body.get("decision")
            if decision not in ("accept", "reject", "defer"):
                raise _Reject(400, "decision must be accept, reject or defer")
            note = body.get("note") or ""
            if not isinstance(note, str) or len(note) > MAX_QUESTION_CHARS:
                raise _Reject(400, "invalid note")
            entry = state.mailbox.write_decision(finding_id, decision, note)
            row = InboxRow(id=new_question_id(), ts=entry.ts, target=state.slug,
                           kind="decision", finding_id=finding_id, text=note,
                           decision=decision)
            state.mailbox.append_inbox(row)
            past = {"accept": "accepted", "reject": "rejected", "defer": "deferred"}[decision]
            log_event(COMPONENT, "decision", f"finding {finding_id} {past}",
                      session_id=viewer.session_id, target=state.slug,
                      finding_id=finding_id, decision=decision)
            self._json({"id": row.id, "decision": entry.model_dump(mode="json")})

    return Handler


def build_viewer(findings: list[tuple[FindingsFile, Path]], *, agent: str, session_id: str,
                 idle_minutes: float, meta: dict[str, dict] | None = None) -> Viewer:
    """`meta` maps a target slug to `{"pr", "notice", "stats"}`: PR metadata
    for the page header, the note that a review exists for other commits, and
    the measured badges (`stats.change_stats`). None goes into findings.json."""
    targets: dict[str, TargetState] = {}
    for ff, box in findings:
        mailbox = Mailbox(box)
        mailbox.create()
        extra = (meta or {}).get(ff.target.slug, {})
        targets[ff.target.slug] = TargetState(ff, mailbox, allowed_paths(ff.target, ff),
                                              pr=extra.get("pr"), notice=extra.get("notice"),
                                              stats=extra.get("stats"))
    return Viewer(targets=targets, token=secrets.token_urlsafe(32), agent=agent,
                  session_id=session_id, idle_minutes=idle_minutes)


def bind(viewer: Viewer, host: str, first_port: int, span: int) -> ViewerHTTPServer | None:
    handler = make_handler(viewer)
    for port in range(first_port, first_port + span):
        try:
            viewer.httpd = ViewerHTTPServer((host, port), handler)
            return viewer.httpd
        except OSError:
            continue
    return None


def open_page_html(url: str, token: str) -> str:
    target = escape(f"{url}?t={token}", quote=True)
    return ("<!doctype html>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"referrer\" content=\"no-referrer\">\n"
            f"<meta http-equiv=\"refresh\" content=\"0; url={target}\">\n"
            "<title>Opening review viewer…</title>\n"
            f"<p><a href=\"{target}\" rel=\"noreferrer\">Open the review viewer</a></p>\n")


def publish(viewer: Viewer, urls: list[tuple[str, str]]) -> None:
    """Write the token file, the tokenised redirect page and the server record
    into every mailbox, and create an empty inbox so `tail -F` has a file."""
    record = {"url_path_only": "/", "port": viewer.port, "pid": os.getpid(),
              "session_id": viewer.session_id, "started": viewer.started,
              "targets": list(viewer.targets)}
    for state in viewer.targets.values():
        box = state.mailbox
        write_private(box.token_file, viewer.token + "\n")
        write_private(box.open_html, open_page_html(urls[0][0], viewer.token))
        write_private(box.server_json, json.dumps(record, indent=2) + "\n")
        # What `walkthrough put` checks against. Kept after exit: no secret.
        write_private(served_path(box.dir),
                      Served(state.findings, state.pr, state.notice, state.stats).to_json())
    registry.register(os.getpid(), [s.mailbox.dir.resolve() for s in viewer.targets.values()])


def unpublish(viewer: Viewer) -> None:
    for state in viewer.targets.values():
        for path in (state.mailbox.token_file, state.mailbox.open_html,
                     state.mailbox.server_json):
            try:
                path.unlink()
            except OSError:
                pass
    registry.unregister(os.getpid())


def watchdog(viewer: Viewer) -> None:
    """Stop the server when no page has been open for `idle_minutes`.

    The page sends a heartbeat (`/api/alive`) every 10 s while it is open, so
    reading for half an hour without clicking keeps it alive, and closing the
    tab stops it. A browser cannot reliably announce that a tab closed, so the
    silence is the signal.
    """
    idle_s = viewer.idle_minutes * 60
    step = max(0.05, min(30.0, idle_s / 4))
    while not viewer.stopped.wait(step):
        quiet = time.monotonic() - viewer.last_seen
        if quiet >= idle_s:
            viewer.stop_reason = "idle"
            minutes = viewer.idle_minutes
            span = f"{minutes:g} min"
            log.info("no request for %.0f s; stopping", quiet)
            log_event(COMPONENT, "idle_stop", f"viewer stopped after {span} with no open page",
                      session_id=viewer.session_id, idle_minutes=minutes)
            viewer.httpd.shutdown()
            return


def run(viewer: Viewer) -> None:
    """Serve until shutdown, idle timeout or a signal; always clean up."""
    assert viewer.httpd is not None
    if viewer.idle_minutes > 0:
        threading.Thread(target=watchdog, args=(viewer,), daemon=True).start()
    try:
        viewer.httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        viewer.stop_reason = viewer.stop_reason or "signal"
        log_event(COMPONENT, "stop", "viewer stopped by a signal",
                  session_id=viewer.session_id, reason="signal")
    finally:
        viewer.stopped.set()
        viewer.httpd.server_close()
        unpublish(viewer)
        log.info("stopped (%s)", viewer.stop_reason or "unknown")
