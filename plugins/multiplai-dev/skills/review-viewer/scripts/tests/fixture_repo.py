"""Build the small git repository the tests (and the live check) review.

Two commits. Between them: one file modified (`app/service.py`), one added
(`app/new_feature.py`), one deleted (`app/old_module.py`, 150 lines), one
binary file modified (`assets/logo.bin`) and one 4500-line file modified
(`app/big.py`). Author, committer and dates are fixed, so the commit shas are
the same on every machine and `fixtures/findings.example.json` can name them.

    python tests/fixture_repo.py <dir>     # build it somewhere, print the shas

`build_remote()` adds what target resolution needs, on top of those two
commits: a bare `origin` (with `origin/HEAD` → `main`), a `feature` branch
that `main` moved past after it was cut, one commit on `feature` that is not
pushed, and a PR head pushed only to `refs/pull/7/head` on the origin, with a
fresh clone that does not have it yet.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SERVICE_BASE = '''"""Order service."""


def total(items):
    return sum(i["price"] for i in items)


def apply_discount(amount, pct):
    return amount - amount * pct / 100


def refund(order):
    order["status"] = "refunded"
    return order
'''

SERVICE_HEAD = '''"""Order service."""

import logging

log = logging.getLogger(__name__)


def total(items):
    return sum(i["price"] * i["qty"] for i in items)


def apply_discount(amount, pct):
    if pct > 100:
        pct = 100
    return amount - amount * pct / 100


def refund(order):
    order["status"] = "refunded"
    log.info("refunded %s", order["id"])
    return order
'''

NEW_FEATURE = '''"""Loyalty points."""


def points_for(amount):
    return int(amount // 10)
'''

OLD_MODULE = "".join(f"LEGACY_{i} = {i}\n" for i in range(1, 151))
BIG_BASE = "".join(f"value_{i} = {i}\n" for i in range(1, 4501))
BIG_HEAD = BIG_BASE.replace("value_2000 = 2000\n", "value_2000 = 'changed'\n")


def _git(repo: Path, *args: str, date: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.com",
               GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.com",
               GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date, GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CONFIG_NOSYSTEM="1")
    return subprocess.run(["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args],
                          check=True, capture_output=True, text=True, env=env,
                          stdin=subprocess.DEVNULL).stdout.strip()


def _write(repo: Path, rel: str, data: str | bytes) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")


def build(repo: Path) -> tuple[str, str]:
    """Create the repository at `repo`; return (base_sha, head_sha)."""
    repo.mkdir(parents=True, exist_ok=True)
    d1, d2 = "2026-09-01T10:00:00+00:00", "2026-09-02T10:00:00+00:00"
    _git(repo, "init", "-q", "-b", "main", date=d1)
    _write(repo, "app/service.py", SERVICE_BASE)
    _write(repo, "app/old_module.py", OLD_MODULE)
    _write(repo, "app/big.py", BIG_BASE)
    _write(repo, "assets/logo.bin", bytes(range(256)) * 4)
    _git(repo, "add", "-A", date=d1)
    _git(repo, "commit", "-q", "-m", "base", date=d1)
    base = _git(repo, "rev-parse", "HEAD", date=d1)

    _write(repo, "app/service.py", SERVICE_HEAD)
    _write(repo, "app/new_feature.py", NEW_FEATURE)
    (repo / "app/old_module.py").unlink()
    _write(repo, "app/big.py", BIG_HEAD)
    _write(repo, "assets/logo.bin", bytes(reversed(range(256))) * 4)
    _git(repo, "add", "-A", date=d2)
    _git(repo, "commit", "-q", "-m", "head", date=d2)
    head = _git(repo, "rev-parse", "HEAD", date=d2)
    return base, head


FEATURE_FILE = '''"""Feature work."""


def feature():
    return 1
'''


def build_remote(root: Path) -> dict[str, object]:
    """The layout described in the module docstring. Returns paths and shas."""
    root.mkdir(parents=True, exist_ok=True)
    work, origin, clone = root / "work", root / "origin.git", root / "clone"
    base, head = build(work)
    d = "2026-09-03T10:00:00+00:00"
    _git(root, "init", "-q", "--bare", "-b", "main", str(origin), date=d)
    _git(work, "remote", "add", "origin", str(origin), date=d)
    _git(work, "push", "-q", "origin", "main", date=d)
    _git(work, "fetch", "-q", "origin", date=d)
    _git(work, "remote", "set-head", "origin", "main", date=d)

    _git(work, "checkout", "-q", "-b", "feature", date=d)
    _write(work, "app/feature.py", FEATURE_FILE)
    _git(work, "add", "-A", date=d)
    _git(work, "commit", "-q", "-m", "feature: pushed", date=d)
    feature_pushed = _git(work, "rev-parse", "HEAD", date=d)
    _git(work, "push", "-q", "-u", "origin", "feature", date=d)

    d2 = "2026-09-04T10:00:00+00:00"
    _git(work, "checkout", "-q", "main", date=d2)
    _write(work, "README.md", "main moved on\n")
    _git(work, "add", "-A", date=d2)
    _git(work, "commit", "-q", "-m", "main: after feature was cut", date=d2)
    main_tip = _git(work, "rev-parse", "HEAD", date=d2)
    _git(work, "push", "-q", "origin", "main", date=d2)

    d3 = "2026-09-05T10:00:00+00:00"
    _git(work, "checkout", "-q", "feature", date=d3)
    _write(work, "app/feature.py", FEATURE_FILE + "\n\ndef more():\n    return 2\n")
    _git(work, "commit", "-q", "-am", "feature: not pushed", date=d3)
    feature_local = _git(work, "rev-parse", "HEAD", date=d3)

    # A PR head that exists on the origin only under refs/pull/7/head.
    d4 = "2026-09-06T10:00:00+00:00"
    _git(work, "checkout", "-q", "--detach", head, date=d4)
    _write(work, "app/pr.py", "PR = 7\n")
    _git(work, "add", "-A", date=d4)
    _git(work, "commit", "-q", "-m", "pr 7", date=d4)
    pr_head = _git(work, "rev-parse", "HEAD", date=d4)
    _git(work, "push", "-q", "origin", "HEAD:refs/pull/7/head", date=d4)
    _git(work, "checkout", "-q", "feature", date=d4)

    _git(root, "clone", "-q", "--no-local", str(origin), str(clone), date=d4)
    return {"work": work, "origin": origin, "clone": clone, "base": base, "head": head,
            "feature_pushed": feature_pushed, "feature_local": feature_local,
            "main_tip": main_tip, "pr_head": pr_head}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python tests/fixture_repo.py <dir>")
    b, h = build(Path(sys.argv[1]).resolve())
    print(f"base {b}\nhead {h}")
