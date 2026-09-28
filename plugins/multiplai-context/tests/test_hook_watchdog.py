"""Tests for scripts/lib/hook_watchdog.py and how context_manager arms it."""

import json
import logging
import os
import re
import socket
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch


PLUGIN_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PLUGIN_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib import hook_watchdog as hw  # noqa: E402


class TestApiHost:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        assert hw.api_host() == ("api.anthropic.com", 443)

    def test_base_url_override(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://proxy.local:8080/v1")
        assert hw.api_host() == ("proxy.local", 8080)

    def test_base_url_without_scheme(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "gateway.example")
        assert hw.api_host() == ("gateway.example", 443)


class TestProbeNetwork:
    def test_reports_dns_and_tcp_times_against_a_local_listener(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        try:
            out = hw.probe_network("127.0.0.1", server.getsockname()[1])
        finally:
            server.close()
        assert re.fullmatch(r"dns=\d+ms tcp=\d+ms", out), out

    def test_names_the_resolver_error(self):
        err = socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")
        with patch.object(hw.socket, "getaddrinfo", side_effect=err):
            out = hw.probe_network("api.anthropic.com", 443)
        assert out == "dns=error:EAI_AGAIN tcp=skipped"

    def test_a_hung_resolver_is_abandoned_at_the_timeout(self):
        release = threading.Event()

        def _hang(*a, **k):
            release.wait(5)
            raise OSError("late")

        started = time.monotonic()
        with patch.object(hw.socket, "getaddrinfo", side_effect=_hang):
            out = hw.probe_network("api.anthropic.com", 443, timeout_s=0.2)
        release.set()
        assert out == "dns=timeout>0.2s tcp=skipped"
        assert time.monotonic() - started < 1.5

    def test_refused_connect_is_reported(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()  # nothing listens on port now
        out = hw.probe_network("127.0.0.1", port)
        assert re.fullmatch(r"dns=\d+ms tcp=error:ConnectionRefusedError", out), out


class TestSdkLog:
    def test_picks_the_newest_file_written_during_the_run(self, tmp_path):
        sdk = tmp_path / "sdk"
        sdk.mkdir()
        old = sdk / "old.log"
        old.write_text("old\n")
        os.utime(old, (time.time() - 3600, time.time() - 3600))
        a = sdk / "a.log"
        a.write_text("a\n")
        b = sdk / "b.log"
        b.write_text("b\n")
        os.utime(a, (time.time() - 5, time.time() - 5))
        assert hw.latest_sdk_log(tmp_path, time.time() - 60) == b

    def test_none_when_nothing_was_written_this_run(self, tmp_path):
        sdk = tmp_path / "sdk"
        sdk.mkdir()
        old = sdk / "old.log"
        old.write_text("x\n")
        os.utime(old, (time.time() - 3600, time.time() - 3600))
        assert hw.latest_sdk_log(tmp_path, time.time() - 60) is None
        assert hw.latest_sdk_log(tmp_path / "missing", 0) is None

    def test_tail_keeps_the_last_lines_and_caps_their_length(self, tmp_path):
        path = tmp_path / "x.log"
        path.write_text("one\n\ntwo\nthree\n" + "z" * 1000 + "\n")
        got = hw.tail(path)
        assert got[:2] == ["two", "three"]
        assert got[2] == "z" * hw.TAIL_LINE_MAX_CHARS


class _Run:
    def __init__(self, elapsed_ms):
        self.elapsed_ms = elapsed_ms


class TestOnWatchdog:
    def test_logs_the_sdk_log_its_tail_and_the_probe(self, tmp_path, caplog, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        sdk = tmp_path / "sdk"
        sdk.mkdir()
        log = sdk / "20260927T003848Z-memory-router-model_client-p1-a1.log"
        log.write_text(
            "[DEBUG] [API REQUEST] /v1/messages x-client-request-id=abc source=sdk\n"
        )
        logger = logging.getLogger("test.hook_watchdog")
        with patch.object(hw, "probe_network", return_value="dns=3ms tcp=40ms"), \
                caplog.at_level(logging.WARNING, logger="test.hook_watchdog"):
            hw.make_on_watchdog(logger, lambda: tmp_path)(_Run(elapsed_ms=26_000))
        msgs = [r.getMessage() for r in caplog.records]
        assert msgs[0] == (
            f"HOOK_WATCHDOG_DETAIL sdk_log={log} api=api.anthropic.com:443 "
            "dns=3ms tcp=40ms"
        )
        assert "x-client-request-id=abc" in msgs[1]

    def test_no_sdk_log_is_said_explicitly(self, tmp_path, caplog):
        logger = logging.getLogger("test.hook_watchdog2")
        with patch.object(hw, "probe_network", return_value="dns=1ms tcp=1ms"), \
                caplog.at_level(logging.WARNING, logger="test.hook_watchdog2"):
            hw.make_on_watchdog(logger, lambda: tmp_path)(_Run(elapsed_ms=26_000))
        assert "sdk_log=none-this-run" in caplog.records[0].getMessage()

    def test_a_failing_logs_dir_still_probes(self, caplog):
        logger = logging.getLogger("test.hook_watchdog3")

        def _boom():
            raise OSError("no data dir")

        with patch.object(hw, "probe_network", return_value="dns=1ms tcp=1ms"), \
                caplog.at_level(logging.WARNING, logger="test.hook_watchdog3"):
            hw.make_on_watchdog(logger, _boom)(_Run(elapsed_ms=26_000))
        assert "dns=1ms tcp=1ms" in caplog.records[0].getMessage()


class TestContextManagerArmsIt:
    def test_watchdog_fires_with_room_before_the_harness_timeout(self):
        """The watchdog plus the worst-case probe must finish before the kill."""
        hooks = json.loads((PLUGIN_DIR / "hooks" / "hooks.json").read_text())
        timeouts = [
            h.get("timeout")
            for groups in hooks["hooks"].values()
            for g in groups
            for h in g["hooks"]
            if "context_manager.py" in h.get("command", "")
        ]
        assert timeouts, "context_manager hook not found in hooks.json"
        src = (SCRIPTS_DIR / "context_manager.py").read_text()
        watchdog_s = float(re.search(r"^_WATCHDOG_S = ([\d.]+)", src, re.M).group(1))
        worst_probe_s = 2 * hw.PROBE_TIMEOUT_S
        for timeout in timeouts:
            assert watchdog_s + worst_probe_s + 1.0 <= timeout, (
                f"watchdog at {watchdog_s}s + probes {worst_probe_s}s leaves "
                f"under 1s before the {timeout}s harness kill"
            )

    def test_router_and_core_loggers_reach_context_manager_log(self):
        src = (SCRIPTS_DIR / "context_manager.py").read_text()
        assert re.search(
            r'setup_logging\(\s*"context_manager",\s*propagate_loggers=\("multiplai_core", "lib"\)',
            src,
        ), "context_manager must forward multiplai_core and lib loggers to its file"
        assert "**_watchdog_kwargs()" in src

    def test_watchdog_is_armed_on_a_core_that_supports_it(self, monkeypatch):
        import context_manager as cm

        def _new_hook_run(name, logger, *, session_id=None, watchdog_s=None,
                          on_watchdog=None):
            pass

        monkeypatch.setattr(cm, "hook_run", _new_hook_run)
        kwargs = cm._watchdog_kwargs()
        assert kwargs["watchdog_s"] == cm._WATCHDOG_S
        assert callable(kwargs["on_watchdog"])

    def test_older_core_gets_no_watchdog_rather_than_a_crash(self, monkeypatch):
        import context_manager as cm

        def _old_hook_run(name, logger, *, session_id=None):
            pass

        monkeypatch.setattr(cm, "hook_run", _old_hook_run)
        assert cm._watchdog_kwargs() == {}
