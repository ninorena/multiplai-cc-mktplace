"""What a hook logs just before the harness kills it.

``hook_run(..., watchdog_s=, on_watchdog=)`` in multiplai-core writes a
``HOOK_WATCHDOG`` line naming the stage still running when a hook nears its
harness timeout, then calls ``on_watchdog(run)``. :func:`make_on_watchdog`
builds that callback for hooks that call the model: it adds the evidence that
answers "why was the model call slow", which on 2026-09-27 could not be
answered at all because nothing but ``HOOK_ENTRY`` was left behind.

It logs one ``HOOK_WATCHDOG_DETAIL`` line with:

- the newest SDK CLI debug log that ``run_agent`` wrote during this run
  (``<logs_dir>/sdk/*.log``) and its last few lines, which show the step the
  CLI was on: auth check, request dispatched with its ``x-client-request-id``,
  waiting for the first stream chunk, retrying;
- a DNS lookup and a TCP connect to the API host, each timed and bounded, to
  tell "the network is down" (the 2026-09-24 kills were EAI_AGAIN/ENOTFOUND)
  from "the request is in flight and the API has not answered".

Everything here runs on the watchdog's timer thread seconds before a kill, so
every step is bounded and nothing raises.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_API_HOST = "api.anthropic.com"
PROBE_TIMEOUT_S = 1.2
TAIL_LINES = 3
TAIL_LINE_MAX_CHARS = 240
_TAIL_READ_BYTES = 8192


def api_host() -> tuple[str, int]:
    """The host and port model calls go to (``ANTHROPIC_BASE_URL`` if set)."""
    base = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
    if base:
        parsed = urlparse(base if "://" in base else f"https://{base}")
        if parsed.hostname:
            default_port = 80 if parsed.scheme == "http" else 443
            return parsed.hostname, parsed.port or default_port
    return DEFAULT_API_HOST, 443


def probe_network(
    host: str, port: int, timeout_s: float = PROBE_TIMEOUT_S
) -> str:
    """``dns=<ms|error> tcp=<ms|error>`` for *host*:*port*, each step bounded.

    ``getaddrinfo`` has no timeout of its own, so it runs on a daemon thread
    that is abandoned if it has not answered within *timeout_s*.
    """
    result: dict = {}

    def _resolve() -> None:
        started = time.monotonic()
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            result["addr"] = infos[0][4]
            result["family"] = infos[0][0]
            result["dns_ms"] = (time.monotonic() - started) * 1000
        except Exception as exc:  # noqa: BLE001
            result["dns_err"] = _error_name(exc)

    worker = threading.Thread(target=_resolve, daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        return f"dns=timeout>{timeout_s:.1f}s tcp=skipped"
    if "dns_err" in result:
        return f"dns=error:{result['dns_err']} tcp=skipped"
    dns = f"dns={result['dns_ms']:.0f}ms"

    started = time.monotonic()
    try:
        with socket.socket(result["family"], socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout_s)
            sock.connect(result["addr"])
        tcp = f"tcp={(time.monotonic() - started) * 1000:.0f}ms"
    except socket.timeout:
        tcp = f"tcp=timeout>{timeout_s:.1f}s"
    except Exception as exc:  # noqa: BLE001
        tcp = f"tcp=error:{_error_name(exc)}"
    return f"{dns} {tcp}"


def _error_name(exc: BaseException) -> str:
    if isinstance(exc, socket.gaierror) and exc.args:
        # EAI_AGAIN / EAI_NONAME, as the CLI's own api_error entries name them.
        for name in dir(socket):
            if name.startswith("EAI_") and getattr(socket, name) == exc.args[0]:
                return name
    return type(exc).__name__


def latest_sdk_log(logs_dir: Path, since_epoch: float) -> Path | None:
    """The newest ``sdk/*.log`` modified at or after *since_epoch*, if any."""
    newest: tuple[float, Path] | None = None
    try:
        for path in (logs_dir / "sdk").glob("*.log"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime >= since_epoch and (newest is None or mtime > newest[0]):
                newest = (mtime, path)
    except OSError:
        return None
    return newest[1] if newest else None


def tail(path: Path, lines: int = TAIL_LINES) -> list[str]:
    """The last *lines* non-empty lines of *path*, each capped in length."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - _TAIL_READ_BYTES))
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    kept = [ln.strip() for ln in data.splitlines() if ln.strip()][-lines:]
    return [ln[:TAIL_LINE_MAX_CHARS] for ln in kept]


def make_on_watchdog(logger: logging.Logger, logs_dir_fn):
    """Build the ``on_watchdog(run)`` callback for a model-calling hook.

    *logs_dir_fn* returns the multiplai logs directory; it is called on the
    timer thread, so a failure there costs only the SDK-log part of the line.
    """

    def _on_watchdog(run) -> None:
        started_epoch = time.time() - run.elapsed_ms / 1000.0
        parts = ["HOOK_WATCHDOG_DETAIL"]
        sdk_log = None
        try:
            sdk_log = latest_sdk_log(Path(logs_dir_fn()), started_epoch)
        except Exception:  # noqa: BLE001
            pass
        parts.append(f"sdk_log={sdk_log if sdk_log else 'none-this-run'}")
        host, port = api_host()
        parts.append(f"api={host}:{port}")
        parts.append(probe_network(host, port))
        logger.warning(" ".join(parts))
        if sdk_log is not None:
            for line in tail(sdk_log):
                logger.warning("HOOK_WATCHDOG_DETAIL sdk_log_tail: %s", line)

    return _on_watchdog
