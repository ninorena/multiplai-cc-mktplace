from __future__ import annotations

import json
import subprocess
from sys import executable as PYTHON

import pytest

from review_viewer.mailbox import Mailbox, MailboxError, new_question_id, utc_now
from review_viewer.models import InboxRow, OutboxRow


@pytest.fixture
def box(tmp_path) -> Mailbox:
    mb = Mailbox(tmp_path / "viewer")
    mb.create()
    return mb


def _question(box: Mailbox, text: str = "why?") -> InboxRow:
    row = InboxRow(id=new_question_id(), ts=utc_now(), target="t", kind="question", text=text)
    box.append_inbox(row)
    return row


def test_append_and_read_outbox(box):
    for i in range(3):
        box.append_outbox(OutboxRow(reply_to="q", ts=utc_now(), text=f"part {i}", done=i == 2))
    rows, n = box.read_outbox(0)
    assert n == 3 and [r["text"] for r in rows] == ["part 0", "part 1", "part 2"]
    rows, n = box.read_outbox(2)
    assert n == 3 and [r["done"] for r in rows] == [True]


def test_truncated_trailing_line_is_skipped(box):
    box.append_outbox(OutboxRow(reply_to="q", ts=utc_now(), text="whole", done=True))
    with box.outbox.open("a", encoding="utf-8") as fh:
        fh.write('{"v":1,"reply_to":"q","ts":"2026')  # a writer caught mid-line
    rows, n = box.read_outbox(0)
    assert n == 1 and rows[0]["text"] == "whole"


def test_row_size_limit(box):
    with pytest.raises(MailboxError):
        box.append_outbox(OutboxRow(reply_to="q", ts=utc_now(), text="x" * 70000, done=True))


def test_decisions_keep_latest(box):
    box.write_decision("abc", "accept", "looks right")
    box.write_decision("abc", "reject", "changed my mind")
    box.write_decision("def", "defer")
    data = box.read_decisions()
    assert data["abc"]["decision"] == "reject" and data["abc"]["note"] == "changed my mind"
    assert data["def"]["decision"] == "defer"
    assert not list(box.dir.glob(".decisions.json.*")), "temp file left behind"


def test_viewed_marks_and_unmarks(box):
    box.set_viewed("a.py", True)
    data = box.set_viewed("b/c.py", True)
    assert set(data) == {"a.py", "b/c.py"}
    assert box.set_viewed("a.py", False) == {"b/c.py": data["b/c.py"]}
    assert box.read_viewed() == {"b/c.py": data["b/c.py"]}
    assert not list(box.dir.glob(".viewed.json.*")), "temp file left behind"


def test_viewed_ignores_a_damaged_file(box):
    box.viewed.write_text("[1, 2]")
    assert box.read_viewed() == {}
    box.viewed.write_text('{"a.py": 3, "b.py": "2026-01-01T00:00:00Z"}')
    assert box.read_viewed() == {"b.py": "2026-01-01T00:00:00Z"}


def _reply(*args, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([PYTHON, "-m", "review_viewer", "reply", *args],
                          input=stdin, capture_output=True, text=True)


def test_reply_reads_60k_answer_from_stdin(box):
    q = _question(box)
    answer = ("A line of the answer with `code` and **bold**.\n" * 1400)[: 60 * 1024]
    proc = _reply("--box", str(box.dir), "--to", q.id, stdin=answer)
    assert proc.returncode == 0, proc.stderr
    rows, _ = box.read_outbox(0)
    assert "".join(r["text"] for r in rows) == answer
    assert all(r["reply_to"] == q.id for r in rows)
    assert rows[-1]["done"] is True
    assert all(len(json.dumps(r).encode()) < 64 * 1024 for r in rows)


def test_reply_splits_oversized_answer(box):
    q = _question(box)
    answer = "\n".join(f"line {i} " + "é" * 60 for i in range(3000))
    proc = _reply("--box", str(box.dir), "--to", q.id, stdin=answer)
    assert proc.returncode == 0, proc.stderr
    rows, _ = box.read_outbox(0)
    assert len(rows) > 1
    assert "".join(r["text"] for r in rows) == answer
    assert [r["done"] for r in rows] == [False] * (len(rows) - 1) + [True]


def test_reply_more_and_file(box, tmp_path):
    q = _question(box)
    md = tmp_path / "answer.md"
    md.write_text("Looking at `app/service.py:9`…", encoding="utf-8")
    assert _reply("--box", str(box.dir), "--to", q.id, "--more", "--file", str(md)).returncode == 0
    assert _reply("--box", str(box.dir), "--to", q.id, stdin="Done.").returncode == 0
    rows, _ = box.read_outbox(0)
    assert [(r["text"], r["done"]) for r in rows] == [
        ("Looking at `app/service.py:9`…", False), ("Done.", True)]


def test_reply_unknown_id_refused(box):
    _question(box)
    proc = _reply("--box", str(box.dir), "--to", "q-nope", stdin="hi")
    assert proc.returncode == 2
    assert "no question 'q-nope'" in proc.stderr
    assert box.read_outbox(0) == ([], 0)


def test_reply_missing_box_refused(tmp_path):
    proc = _reply("--box", str(tmp_path / "typo"), "--to", "q-1", stdin="hi")
    assert proc.returncode == 2
    assert not (tmp_path / "typo").exists()


def test_rows_survive_unicode_line_separators(box):
    text = "first\u2028second\u2029third\x85fourth\x0cfifth"
    box.append_outbox(OutboxRow(reply_to="q", ts=utc_now(), text=text, done=True))
    rows, n = box.read_outbox(0)
    assert n == 1 and rows[0]["text"] == text
    q = _question(box, "why\u2028this?")
    proc = _reply("--box", str(box.dir), "--to", q.id, stdin="because")
    assert proc.returncode == 0, proc.stderr


def test_mailbox_is_private(box):
    import stat
    box.append_outbox(OutboxRow(reply_to="q", ts=utc_now(), text="a", done=True))
    _question(box)
    box.write_decision("abc", "accept")
    box.set_viewed("a.py", True)
    assert stat.S_IMODE(box.dir.stat().st_mode) == 0o700
    for path in (box.inbox, box.outbox, box.decisions, box.viewed):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path.name


def test_create_tightens_existing_files(tmp_path):
    import stat
    d = tmp_path / "old"
    d.mkdir(mode=0o755)
    (d / "outbox.jsonl").write_text("")
    (d / "outbox.jsonl").chmod(0o644)
    Mailbox(d).create()
    assert stat.S_IMODE((d / "outbox.jsonl").stat().st_mode) == 0o600
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
