"""The daily learnings review: queues, rank, merge, reserve, review file, apply.

The property that matters most is that no path loses an entry. Several tests
count entries before and after and compare.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from lib import daily_review as dr

TODAY = "2026-09-30"


def line(desc, target="technical-pref.md", trust="verified", typ="OBSERVATION", action="do it"):
    return f"- **[trust: {trust}]** {typ} {desc} → Target: {target} — {action}"


def raw(*records):
    """records: (timestamp, session, [lines])"""
    out = []
    for ts, sid, lines in records:
        out.append(f"## Session Learnings — {ts}\nSession: {sid}\n" + "\n".join(lines) + "\n\n---")
    return "\n".join(out) + "\n"


@pytest.fixture
def env(tmp_path):
    ws = tmp_path / "ws"
    mem = ws / ".multiplai" / "memory"
    learn = ws / ".multiplai" / "learnings"
    dreams = ws / ".multiplai" / "dreams"
    for d in (mem, learn, dreams):
        d.mkdir(parents=True)
    dirs = dr.Dirs(workspace=ws, memory=mem, learnings=learn, dreams=dreams)
    return dirs, dr.Store(learn)


def put(dirs, name, text):
    (dirs.learnings / name).write_text(text)


def total_entries(store):
    n = 0
    for t in store.queue_targets():
        n += len(store.queue(t))
    for p in (store.reserve_path, store.rejected_path, store.applied_path):
        n += len(store.read(p))
    return n


def word_salad(i):
    return f"unique subject number{i} alpha{i} beta{i} gamma{i} delta{i}"


# ---------------------------------------------------------------- ingest


class TestIngest:
    def test_entries_are_stored_and_the_raw_file_is_archived(self, env):
        dirs, store = env
        put(dirs, "2026-09-29.md", raw(("2026-09-29T10:00:00", "s1", [line("first fact"), line("other", "me.md")])))
        rep = dr.ingest(store)
        assert rep.entries == 2 and rep.files_archived == ["2026-09-29.md"]
        assert not (dirs.learnings / "2026-09-29.md").exists()
        assert (dirs.learnings / "archived" / "2026-09-29.md").exists()
        assert len(store.queue("technical-pref.md")) == 1 and len(store.queue("me.md")) == 1

    def test_a_file_with_a_line_it_cannot_parse_stays_put(self, env):
        dirs, store = env
        text = raw(("2026-09-29T10:00:00", "s1", [line("ok fact")])) + "stray prose nobody parsed\n"
        put(dirs, "a.md", text)
        rep = dr.ingest(store)
        assert rep.files_held and rep.files_archived == []
        assert (dirs.learnings / "a.md").read_text() == text
        assert total_entries(store) == 0

    def test_empty_session_blocks_are_fine(self, env):
        dirs, store = env
        put(dirs, "a.md", raw(("t1", "s1", []), ("t2", "s1", [line("real")])))
        assert dr.ingest(store).entries == 1

    def test_ingest_twice_is_idempotent(self, env):
        dirs, store = env
        text = raw(("2026-09-29T10:00:00", "s1", [line("same fact")]))
        put(dirs, "a.md", text)
        dr.ingest(store)
        put(dirs, "a.md", text)        # the same file arrives again
        dr.ingest(store)
        q = store.queue("technical-pref.md")
        assert len(q) == 1 and q[0].meta["seen"] == 2
        assert (dirs.learnings / "archived" / "a-2.md").exists()

    def test_a_failed_store_keeps_the_raw_file(self, env, monkeypatch):
        dirs, store = env
        put(dirs, "a.md", raw(("t", "s1", [line("fact")])))
        monkeypatch.setattr(dr, "_atomic_write", lambda *a, **k: (_ for _ in ()).throw(PermissionError("no")))
        rep = dr.ingest(store)
        assert (dirs.learnings / "a.md").exists() and rep.files_held

    def test_a_crash_between_store_and_archive_loses_nothing(self, env, monkeypatch):
        dirs, store = env
        put(dirs, "a.md", raw(("t", "s1", [line("fact")])))
        monkeypatch.setattr(dr, "_archive_raw", lambda f: (_ for _ in ()).throw(OSError("crash")))
        dr.ingest(store)
        assert (dirs.learnings / "a.md").exists()
        assert len(store.queue("technical-pref.md")) == 1
        monkeypatch.undo()
        dr.ingest(store)                # the re-run merges instead of duplicating
        assert len(store.queue("technical-pref.md")) == 1


# ---------------------------------------------------------------- merge


class TestMerge:
    def test_near_duplicates_merge_into_one_and_keep_every_wording(self, env):
        dirs, store = env
        put(dirs, "a.md", raw(
            ("2026-09-28T10:00:00", "s1", [line("Nick wants the PLAI monitor cron moved off the top of the hour")]),
            ("2026-09-29T10:00:00", "s2", [line("Nick wants the PLAI monitor cron moved off the top of the hour to avoid drops")]),
            ("2026-09-30T10:00:00", "s3", [line("the PLAI monitor cron should move off the top of the hour")]),
        ))
        dr.ingest(store)
        q = store.queue("technical-pref.md")
        assert len(q) == 1
        assert q[0].meta["seen"] == 3 and len(q[0].meta["sessions"]) == 3
        assert len(q[0].also) == 2
        assert q[0].meta["last"].startswith("2026-09-30")

    def test_different_facts_do_not_merge(self, env):
        dirs, store = env
        put(dirs, "a.md", raw(("t", "s1", [line("alpha bravo charlie delta"), line("echo foxtrot golf hotel")])))
        dr.ingest(store)
        assert len(store.queue("technical-pref.md")) == 2

    def test_same_words_different_target_do_not_merge(self, env):
        dirs, store = env
        put(dirs, "a.md", raw(("t", "s1", [line("the same fact here", "me.md"), line("the same fact here", "project.md")])))
        dr.ingest(store)
        assert len(store.queue("me.md")) == 1 and len(store.queue("project.md")) == 1

    def test_a_duplicate_of_a_rejected_entry_never_returns_to_a_queue(self, env):
        dirs, store = env
        put(dirs, "a.md", raw(("t", "s1", [line("the PLAI cron fact stays rejected")])))
        dr.ingest(store)
        e = store.queue("technical-pref.md")[0]
        store.move(e.key, store.queue_path(e.target), store.rejected_path)
        put(dirs, "b.md", raw(("t2", "s2", [line("the PLAI cron fact stays rejected")])))
        dr.ingest(store)
        assert store.queue("technical-pref.md") == []
        assert store.read(store.rejected_path)[0].meta["seen"] == 2


# ---------------------------------------------------------------- rank


class TestRank:
    def _e(self, key, **m):
        base = {"key": key, "target": "t", "trust": "high", "correction": False,
                "sessions": ["a"], "last": "2026-09-01"}
        base.update(m)
        return dr.Entry(line="- x", meta=base)

    def test_order_is_correction_then_repeat_then_verified_then_newest(self):
        es = [
            self._e("newest", last="2026-09-30"),
            self._e("verified", trust="verified"),
            self._e("repeat", sessions=["a", "b"]),
            self._e("correction", correction=True),
        ]
        assert [e.key for e in dr.rank_sorted(es)] == ["correction", "repeat", "verified", "newest"]

    def test_newest_breaks_ties(self):
        es = [self._e("old", last="2026-09-01"), self._e("new", last="2026-09-20")]
        assert [e.key for e in dr.rank_sorted(es)] == ["new", "old"]


# ---------------------------------------------------------------- limit, reserve, refill, drop


def fill(dirs, store, n, target="technical-pref.md", day="2026-09-29", start=0):
    lines = [line(word_salad(i), target) for i in range(start, start + n)]
    put(dirs, f"f{start}.md", raw((f"{day}T10:00:00", f"s{start}", lines)))
    dr.ingest(store)


class TestQueueLimit:
    def test_no_queue_holds_more_than_ten(self, env):
        dirs, store = env
        fill(dirs, store, 25)
        rep = dr.rebalance(store, today=TODAY)
        assert len(store.queue("technical-pref.md")) == dr.QUEUE_MAX
        assert len(store.reserve("technical-pref.md")) == 15 and rep.to_reserve == 15

    def test_the_lowest_ranked_go_to_the_reserve(self, env):
        dirs, store = env
        fill(dirs, store, 10)
        put(dirs, "c.md", raw(("2026-09-29T11:00:00", "sc", [line("a correction worth seeing", typ="CORRECTION")])))
        dr.ingest(store)
        dr.rebalance(store, today=TODAY)
        keys = [e.description for e in store.queue("technical-pref.md")]
        assert "a correction worth seeing" in keys

    def test_nothing_is_lost_to_the_limit(self, env):
        dirs, store = env
        fill(dirs, store, 25)
        before = total_entries(store)
        dr.rebalance(store, today=TODAY)
        assert total_entries(store) == before == 25

    def test_a_queue_under_ten_refills_from_the_reserve_best_first(self, env):
        dirs, store = env
        fill(dirs, store, 14)
        dr.rebalance(store, today=TODAY)
        assert len(store.reserve()) == 4
        q = store.queue("technical-pref.md")
        for e in q[:6]:
            store.move(e.key, store.queue_path(e.target), store.rejected_path)
        rep = dr.rebalance(store, today=TODAY)
        assert rep.refilled == 4
        assert len(store.queue("technical-pref.md")) == 8 and store.reserve() == []

    def test_refill_takes_only_the_same_target(self, env):
        dirs, store = env
        fill(dirs, store, 12, "technical-pref.md")
        fill(dirs, store, 2, "me.md", start=100)
        dr.rebalance(store, today=TODAY)
        assert len(store.queue("me.md")) == 2
        assert all(e.target == "technical-pref.md" for e in store.reserve())

    def test_reserve_entries_past_ninety_days_go_to_rejected(self, env):
        dirs, store = env
        fill(dirs, store, 12)
        dr.rebalance(store, today="2026-06-01")
        assert len(store.reserve()) == 2
        # the queue is full, so nothing refills; 91 days later the reserve expires
        rep = dr.rebalance(store, today="2026-09-01")
        assert rep.expired == 2 and store.reserve() == []
        rej = store.read(store.rejected_path)
        assert len(rej) == 2 and "over 90 days" in rej[0].meta["reason"]

    def test_entries_inside_ninety_days_stay_in_the_reserve(self, env):
        dirs, store = env
        fill(dirs, store, 12)
        dr.rebalance(store, today="2026-07-01")
        dr.rebalance(store, today="2026-09-28")     # 89 days
        assert len(store.reserve()) == 2

    def test_one_queue_per_target(self, env):
        dirs, store = env
        fill(dirs, store, 3, "me.md")
        fill(dirs, store, 4, "PROJECTS/work/moms-first/context.md", start=50)
        assert sorted(store.queue_targets()) == ["PROJECTS/work/moms-first/context.md", "me.md"]


# ---------------------------------------------------------------- review file


class TestReviewFile:
    def test_at_most_two_per_file(self, env):
        dirs, store = env
        fill(dirs, store, 8, "me.md")
        fill(dirs, store, 8, "project.md", start=50)
        dr.rebalance(store, today=TODAY)
        path, n = dr.build_review(store, dirs, today=TODAY)
        items = dr.parse_review(path.read_text())
        assert n == 4 and len(items) == 4
        for t in ("me.md", "project.md"):
            assert sum(1 for i in items if i.target == t) == 2

    def test_the_file_shows_the_exact_edit_and_three_boxes(self, env):
        dirs, store = env
        (dirs.memory / "me.md").write_text("# me\n")
        fill(dirs, store, 1, "me.md")
        path, _ = dr.build_review(store, dirs, today=TODAY)
        text = path.read_text()
        assert "Add:" in text and "```text" in text
        assert text.count("- [ ] yes") == text.count("- [ ] no") == text.count("- [ ] later") == 1

    def test_the_top_of_the_queue_is_what_you_see(self, env):
        dirs, store = env
        fill(dirs, store, 5, "me.md")
        put(dirs, "c.md", raw(("2026-09-29T11:00:00", "sc", [line("the correction", "me.md", typ="CORRECTION")])))
        dr.ingest(store)
        path, _ = dr.build_review(store, dirs, today=TODAY)
        assert "the correction" in path.read_text()

    def test_an_existing_file_for_the_day_is_not_overwritten(self, env):
        dirs, store = env
        fill(dirs, store, 2, "me.md")
        path, _ = dr.build_review(store, dirs, today=TODAY)
        path.write_text(path.read_text().replace("- [ ] yes", "- [x] yes", 1))
        dr.build_review(store, dirs, today=TODAY)
        assert "- [x] yes" in path.read_text()

    def test_no_entries_no_file(self, env):
        dirs, store = env
        path, n = dr.build_review(store, dirs, today=TODAY)
        assert path is None and n == 0

    def test_rule_entries_carry_a_warning(self, env):
        dirs, store = env
        put(dirs, "a.md", raw(("t", "s", [line("always do X", "CLAUDE.md", typ="RULE-PROPOSAL")])))
        dr.ingest(store)
        path, _ = dr.build_review(store, dirs, today=TODAY)
        assert "standing rule" in path.read_text()


# ---------------------------------------------------------------- apply


def tick(path, n, what):
    """Tick *what* for the n-th entry (1-based) in a review file."""
    text = path.read_text()
    parts = re.split(r"(?m)^(?=### \d+\.)", text)
    parts[n] = parts[n].replace(f"- [ ] {what}", f"- [x] {what}", 1)
    path.write_text("".join(parts))


def review_for(dirs, store, target="me.md", n=2, body="# me\n\n## Notes\n- old\n\n## Other\n- x\n"):
    (dirs.memory / target).write_text(body)
    fill(dirs, store, n, target)
    path, _ = dr.build_review(store, dirs, today=TODAY)
    return path


class TestApply:
    def test_yes_adds_the_text_and_moves_the_entry_to_applied(self, env):
        dirs, store = env
        path = review_for(dirs, store, n=1)
        tick(path, 1, "yes")
        rep = dr.apply_review(path, store, dirs, today=TODAY)
        assert len(rep.applied) == 1
        assert "number0" in (dirs.memory / "me.md").read_text()
        assert store.queue("me.md") == [] and len(store.read(store.applied_path)) == 1
        assert "Result: applied" in path.read_text()

    def test_only_yes_writes(self, env):
        dirs, store = env
        path = review_for(dirs, store, n=2)
        before = (dirs.memory / "me.md").read_text()
        tick(path, 1, "no")
        tick(path, 2, "later")
        rep = dr.apply_review(path, store, dirs, today=TODAY)
        assert (dirs.memory / "me.md").read_text() == before
        assert len(rep.rejected) == 1 and len(store.queue("me.md")) == 1
        assert len(store.read(store.rejected_path)) == 1

    def test_no_tick_leaves_everything(self, env):
        dirs, store = env
        path = review_for(dirs, store, n=2)
        rep = dr.apply_review(path, store, dirs, today=TODAY)
        assert rep.applied == rep.rejected == [] and len(store.queue("me.md")) == 2

    def test_a_half_ticked_file_applies_what_is_ticked(self, env):
        dirs, store = env
        path = review_for(dirs, store, n=2)
        tick(path, 1, "yes")
        dr.apply_review(path, store, dirs, today=TODAY)
        assert len(store.queue("me.md")) == 1
        # the second run applies the second, leaves the first's result alone
        tick(path, 2, "yes")
        rep = dr.apply_review(path, store, dirs, today=TODAY)
        assert len(rep.applied) == 1 and store.queue("me.md") == []

    def test_two_yes_for_one_file_in_one_run_do_not_trip_the_changed_check(self, env):
        dirs, store = env
        path = review_for(dirs, store, n=2)
        tick(path, 1, "yes")
        tick(path, 2, "yes")
        rep = dr.apply_review(path, store, dirs, today=TODAY)
        assert len(rep.applied) == 2 and rep.left == []

    def test_running_twice_does_not_apply_twice(self, env):
        dirs, store = env
        path = review_for(dirs, store, n=1)
        tick(path, 1, "yes")
        dr.apply_review(path, store, dirs, today=TODAY)
        once = (dirs.memory / "me.md").read_text()
        dr.apply_review(path, store, dirs, today=TODAY)
        assert (dirs.memory / "me.md").read_text() == once

    def test_two_boxes_ticked_is_left_alone(self, env):
        dirs, store = env
        path = review_for(dirs, store, n=1)
        tick(path, 1, "yes")
        tick(path, 1, "no")
        rep = dr.apply_review(path, store, dirs, today=TODAY)
        assert rep.applied == rep.rejected == [] and "more than one" in rep.left[0][1]
        assert len(store.queue("me.md")) == 1

    def test_a_target_changed_since_the_review_is_not_written_and_is_shown_again(self, env):
        dirs, store = env
        path = review_for(dirs, store, n=1)
        (dirs.memory / "me.md").write_text("# me\n\nsomeone edited this\n")
        tick(path, 1, "yes")
        rep = dr.apply_review(path, store, dirs, today=TODAY)
        assert rep.applied == [] and "changed" in rep.left[0][1]
        assert (dirs.memory / "me.md").read_text() == "# me\n\nsomeone edited this\n"
        text = path.read_text()
        assert "CHANGED:" in text and "- [ ] yes" in text and "- [x] yes" not in text
        assert len(store.queue("me.md")) == 1
        # ticking again now applies
        tick(path, 1, "yes")
        assert len(dr.apply_review(path, store, dirs, today=TODAY).applied) == 1

    def test_the_user_can_edit_the_add_text_before_ticking(self, env):
        dirs, store = env
        path = review_for(dirs, store, n=1)
        path.write_text(re.sub(r"- unique subject.*", "- my own wording", path.read_text()))
        tick(path, 1, "yes")
        dr.apply_review(path, store, dirs, today=TODAY)
        text = (dirs.memory / "me.md").read_text()
        assert "my own wording" in text and "number0" not in text

    def test_section_places_the_text_at_the_end_of_that_section(self, env):
        dirs, store = env
        path = review_for(dirs, store, n=1)
        path.write_text(path.read_text().replace("Section: END", "Section: Notes"))
        tick(path, 1, "yes")
        dr.apply_review(path, store, dirs, today=TODAY)
        text = (dirs.memory / "me.md").read_text()
        assert text.index("- old") < text.index("number0") < text.index("## Other")

    def test_a_missing_section_leaves_the_entry(self, env):
        dirs, store = env
        path = review_for(dirs, store, n=1)
        path.write_text(path.read_text().replace("Section: END", "Section: Nope"))
        tick(path, 1, "yes")
        rep = dr.apply_review(path, store, dirs, today=TODAY)
        assert "not found" in rep.left[0][1] and len(store.queue("me.md")) == 1

    def test_a_full_file_refuses_and_says_what_to_do(self, env):
        dirs, store = env
        body = "# me\n\n> Cap: 1 KB. Review: 2 entries at a time.\n\n" + ("x" * 1000) + "\n"
        path = review_for(dirs, store, n=1, body=body)
        tick(path, 1, "yes")
        rep = dr.apply_review(path, store, dirs, today=TODAY)
        assert rep.applied == [] and "Name what leaves" in rep.left[0][1]
        assert (dirs.memory / "me.md").read_text() == body
        assert len(store.queue("me.md")) == 1

    def test_a_file_under_its_cap_takes_the_entry(self, env):
        dirs, store = env
        body = "# me\n\n> Cap: 3 KB. Review: 2 entries at a time.\n"
        path = review_for(dirs, store, n=1, body=body)
        tick(path, 1, "yes")
        assert len(dr.apply_review(path, store, dirs, today=TODAY).applied) == 1

    def test_a_missing_target_file_is_not_created(self, env):
        dirs, store = env
        fill(dirs, store, 1, "nope.md")
        path, _ = dr.build_review(store, dirs, today=TODAY)
        tick(path, 1, "yes")
        rep = dr.apply_review(path, store, dirs, today=TODAY)
        assert "does not exist" in rep.left[0][1] and not (dirs.memory / "nope.md").exists()

    def test_a_project_target_resolves_from_the_workspace(self, env):
        dirs, store = env
        t = "PROJECTS/work/x/context.md"
        (dirs.workspace / "PROJECTS/work/x").mkdir(parents=True)
        (dirs.workspace / t).write_text("# x\n")
        fill(dirs, store, 1, t)
        path, _ = dr.build_review(store, dirs, today=TODAY)
        tick(path, 1, "yes")
        dr.apply_review(path, store, dirs, today=TODAY)
        assert "number0" in (dirs.workspace / t).read_text()

    def test_a_target_that_escapes_the_workspace_is_refused(self, env):
        dirs, store = env
        assert dr.resolve_target("../../etc/passwd", dirs) is None
        assert dr.resolve_target("/etc/passwd", dirs) is None

    def test_a_failed_write_keeps_the_entry_in_its_queue(self, env, monkeypatch):
        dirs, store = env
        path = review_for(dirs, store, n=1)
        tick(path, 1, "yes")
        real = dr._atomic_write

        def boom(p, content):
            if Path(p).name == "me.md":
                raise PermissionError("no")
            return real(p, content)

        monkeypatch.setattr(dr, "_atomic_write", boom)
        rep = dr.apply_review(path, store, dirs, today=TODAY)
        assert rep.applied == [] and len(store.queue("me.md")) == 1

    def test_a_failed_move_after_the_write_does_not_lose_the_entry(self, env, monkeypatch):
        dirs, store = env
        path = review_for(dirs, store, n=1)
        tick(path, 1, "yes")
        real_add = dr.Store.add

        def boom(self, p, e):
            if p == self.applied_path:
                raise OSError("disk")
            return real_add(self, p, e)

        monkeypatch.setattr(dr.Store, "add", boom)
        dr.apply_review(path, store, dirs, today=TODAY)
        assert len(store.queue("me.md")) == 1          # still there

    def test_every_entry_is_accounted_for_after_a_mixed_run(self, env):
        dirs, store = env
        fill(dirs, store, 30, "me.md")
        (dirs.memory / "me.md").write_text("# me\n")
        dr.rebalance(store, today=TODAY)
        before = total_entries(store)
        path, _ = dr.build_review(store, dirs, today=TODAY)
        tick(path, 1, "yes")
        tick(path, 2, "no")
        dr.apply_review(path, store, dirs, today=TODAY)
        dr.rebalance(store, today=TODAY)
        assert total_entries(store) == before == 30


def test_the_module_never_unlinks_an_entry_file():
    src = (Path(dr.__file__)).read_text()
    # the only unlinks: a temp file, and the raw file AFTER a hard link to it exists
    assert ".unlink(" not in src.replace("os.unlink(tmp)", "").replace("os.unlink(f)", "")
    assert src.count("os.unlink(f)") == 1 and src.index("os.link(f, dest)") < src.index("os.unlink(f)")
    assert "rmtree" not in src and "os.remove" not in src


# ---------------------------------------------------------------- dream.py wiring


@pytest.fixture
def dream_env(env, monkeypatch):
    from test_dream_skill import _load_dream_module

    dirs, store = env
    dream = _load_dream_module()

    class P:
        learnings_dir = dirs.learnings

        def memory_dir(self):
            return dirs.memory

        def dreams_dir(self):
            return dirs.dreams

    monkeypatch.setattr(dream, "get_paths", lambda: P())
    monkeypatch.setattr(dream, "acquire_run_lock", lambda: True)
    monkeypatch.setattr(dream, "_commit_memory_changes", lambda *a, **k: True)
    return dream, dirs, store


class TestDreamCli:
    def test_daily_review_then_daily_apply(self, dream_env, capsys):
        dream, dirs, store = dream_env
        (dirs.memory / "me.md").write_text("# me\n")
        put(dirs, "a.md", raw(("2026-09-29T10:00:00", "s", [line("fact one", "me.md"), line("other fact two", "me.md")])))
        assert dream._daily_review("2026-09-30") == 0
        review = dirs.dreams / "review-2026-09-30.md"
        assert review.exists()
        tick(review, 1, "yes")
        assert dream._daily_apply(None, "2026-09-30") == 0
        out = capsys.readouterr().out
        assert "Applied 1" in out and "fact one" in (dirs.memory / "me.md").read_text() or "other fact two" in (dirs.memory / "me.md").read_text()

    def test_the_review_file_is_not_mistaken_for_a_proposal(self, dream_env):
        dream, dirs, store = dream_env
        put(dirs, "a.md", raw(("t", "s", [line("fact", "me.md")])))
        dream._daily_review("2026-09-30")
        assert list(dirs.dreams.glob("processed-learnings-*.md")) == []

    def test_daily_apply_without_a_review_file_says_so(self, dream_env, capsys):
        dream, dirs, store = dream_env
        assert dream._daily_apply(None, "2026-09-30") == 1
        assert "No review file" in capsys.readouterr().out

    def test_the_cli_flags_exist(self):
        src = (Path(__file__).parent.parent / "scripts" / "dream.py").read_text()
        assert '"--daily-review"' in src and '"--daily"' in src


def test_an_entry_already_stale_before_our_write_is_not_restamped(env):
    dirs, store = env
    path = review_for(dirs, store, n=2)
    text = path.read_text()
    # make entry 2 look like it was stamped against an older version of the file
    parts = re.split(r"(?m)^(?=### \d+\.)", text)
    parts[2] = re.sub(r'"hash": "[^"]*"', '"hash": "0000000000000000"', parts[2])
    path.write_text("".join(parts))
    tick(path, 1, "yes")
    tick(path, 2, "yes")
    rep = dr.apply_review(path, store, dirs, today=TODAY)
    assert len(rep.applied) == 1 and "changed" in rep.left[0][1]


class TestRealParaphrases:
    """Pairs taken from the real learnings backlog (2026-09-30)."""

    def _pair(self, a, b):
        ea = dr.Entry(line="- x", meta={"key": dr.entry_key(a, "t"), "target": "t"})
        eb = dr.Entry(line="- x", meta={"key": dr.entry_key(b, "t"), "target": "t"})
        ea.line = f"- **[trust: high]** OBSERVATION {a} → Target: t — a"
        eb.line = f"- **[trust: high]** OBSERVATION {b} → Target: t — b"
        return dr.is_duplicate(ea, eb)

    def test_same_fact_two_wordings_merge(self):
        assert self._pair(
            "Colorado FAMLI covers the worker's own serious health condition, including pregnancy-related and postpartum conditions",
            "CO FAMLI covers the worker's own serious health condition (including postpartum); a postpartum case was sent to private insurance",
        )
        assert self._pair(
            "Nick wants all memory file rewrites staged for simultaneous batch review before any memory changes are applied, one row per session is not acceptable",
            "Nick prefers to review all memory rewrites in a single batch rather than approving one row per session",
        )

    def test_different_facts_on_one_topic_stay_apart(self):
        assert not self._pair(
            "Claude Haiku 4.5 scores about 2x higher than GPT-4o-mini on the Intelligence Index while costing more per token",
            "As of 2026-09-24 Haiku 4.5 costs about 7x more per turn than GPT-4o mini but is faster at first word",
        )


def test_the_skills_document_both_commands():
    root = Path(__file__).parent.parent / "skills"
    assert "--daily-review" in (root / "dream" / "SKILL.md").read_text()
    remember = (root / "dream-remember" / "SKILL.md").read_text()
    assert "--daily" in remember and "Do not tick a box for the user" in remember.replace("do not tick a box for the user", "Do not tick a box for the user")


# ---------------------------------------------------------------- findings from the Opus review


class TestReviewFindings:
    def test_merge_keeps_the_other_entrys_whole_line_and_rule_kind(self, env):
        dirs, store = env
        put(dirs, "a.md", raw(
            ("t1", "s1", [line("always use uv run for scripts here", action="add under Tools")]),
            ("t2", "s2", [line("always use uv run for scripts here", typ="RULE-PROPOSAL", action="NEVER use pip, remove the pip section")]),
        ))
        dr.ingest(store)
        q = store.queue("technical-pref.md")
        assert len(q) == 1 and "NEVER use pip" in " ".join(q[0].also)
        assert q[0].meta["kind"] == "RULE-PROPOSAL"

    def test_a_slice_line_does_not_hold_a_file(self, env):
        dirs, store = env
        text = "## Session Learnings — t\nSession: s\nSlice: abc123\n" + line("fact") + "\n\n---\n"
        put(dirs, "a.md", text)
        assert dr.ingest(store).entries == 1

    def test_without_the_extraction_lock_a_fresh_file_is_held(self, env, monkeypatch):
        dirs, store = env
        put(dirs, "a.md", raw(("t", "s", [line("first fact")])))
        monkeypatch.setattr(dr, "_file_lock", _no_lock)
        rep = dr.ingest(store)
        assert rep.files_archived == [] and (dirs.learnings / "a.md").exists()
        assert total_entries(store) == 0

    def test_a_substring_of_an_existing_line_is_not_already_present(self, env):
        dirs, store = env
        body = "# me\n\n- Use uv run for scripts and never pip\n"
        (dirs.memory / "me.md").write_text(body)
        fill(dirs, store, 1, "me.md")
        path, _ = dr.build_review(store, dirs, today=TODAY)
        path.write_text(re.sub(r"- unique subject.*", "- Use uv run for scripts", path.read_text()))
        tick(path, 1, "yes")
        rep = dr.apply_review(path, store, dirs, today=TODAY)
        assert len(rep.applied) == 1 and rep.written_files
        assert "- Use uv run for scripts\n" in (dirs.memory / "me.md").read_text()

    def test_a_heading_inside_a_code_block_is_not_a_section_end(self, env):
        body = "# me\n\n## Tools\n```bash\n# run tests\nmake test\n```\n- after\n\n## Other\n- x\n"
        out = dr._insert(body, "Tools", "- NEW")
        assert out.index("- after") < out.index("- NEW") < out.index("## Other")
        assert out.index("make test") < out.index("- NEW")
        assert dr._insert(body, "run tests", "- x") is None

    def test_written_files_keep_their_mode(self, env):
        dirs, store = env
        path = review_for(dirs, store, n=1)
        os.chmod(dirs.memory / "me.md", 0o644)
        tick(path, 1, "yes")
        dr.apply_review(path, store, dirs, today=TODAY)
        assert oct((dirs.memory / "me.md").stat().st_mode & 0o777) == "0o644"

    def test_the_users_edited_add_text_survives_a_changed_target(self, env):
        dirs, store = env
        path = review_for(dirs, store, n=1)
        path.write_text(re.sub(r"- unique subject.*", "- my careful wording", path.read_text()).replace("Section: END", "Section: Notes"))
        (dirs.memory / "me.md").write_text("# me\n\n## Notes\n- changed\n")
        tick(path, 1, "yes")
        dr.apply_review(path, store, dirs, today=TODAY)
        text = path.read_text()
        assert "my careful wording" in text and "Section: Notes" in text and "CHANGED:" in text

    def test_hand_added_text_in_a_queue_file_is_refused_not_overwritten(self, env):
        dirs, store = env
        fill(dirs, store, 1, "me.md")
        qp = store.queue_path("me.md")
        qp.write_text(qp.read_text() + "\nmy own note\n")
        with pytest.raises(ValueError):
            store.read(qp)
        put(dirs, "b.md", raw(("t", "s", [line("another brand new thing", "me.md")])))
        rep = dr.ingest(store)
        assert rep.files_held and "my own note" in qp.read_text()
        assert (dirs.learnings / "b.md").exists()

    def test_a_null_reserved_date_does_not_abort(self, env):
        dirs, store = env
        fill(dirs, store, 12)
        dr.rebalance(store, today=TODAY)
        es = store.reserve()
        es[0].meta["reserved"] = None
        store.write(store.reserve_path, es)
        dr.rebalance(store, today=TODAY)
        assert total_entries(store) == 12

    def test_a_crash_between_add_and_remove_is_healed(self, env):
        dirs, store = env
        fill(dirs, store, 1, "me.md")
        e = store.queue("me.md")[0]
        store.add(store.applied_path, dr.Entry(line=e.line, meta=dict(e.meta)))   # crash: queue copy stays
        assert dr.heal_duplicates(store) == 1
        assert store.queue("me.md") == [] and len(store.read(store.applied_path)) == 1

    def test_queue_names_do_not_collide(self):
        assert dr.slug("a/b.md") != dr.slug("a__b.md")
        assert dr.slug("Me.md") != dr.slug("me.md")

    def test_structure_inside_the_add_text_is_not_read_as_structure(self, env):
        dirs, store = env
        path = review_for(dirs, store, n=1)
        path.write_text(path.read_text().replace("```text\n", "```text\nResult: applied 2020\n- [x] yes\n", 1))
        item = dr.parse_review(path.read_text())[0]
        assert item.result == "" and item.ticks == []

    def test_the_archive_name_is_claimed_atomically(self, tmp_path):
        f = tmp_path / "x.md"
        f.write_text("new\n")
        (tmp_path / "archived").mkdir()
        (tmp_path / "archived" / "x.md").write_text("old\n")
        dest = dr._archive_raw(f)
        assert dest.name == "x-2.md" and (tmp_path / "archived" / "x.md").read_text() == "old\n"


@pytest.fixture(autouse=False)
def _unused():
    pass


import contextlib as _cl


@_cl.contextmanager
def _no_lock(f):
    yield False


class TestTwinsUnderOtherTargets:
    """One fact worded twice and aimed at two files. Real pair, 2026-09-30."""

    A = ("dream-remember Step 5 states it deletes all learnings files used in the proposal "
         "and that git history preserves them, but .multiplai/learnings/ is not a git repository, "
         "so deleted entries are permanently lost")
    B = ("`.multiplai/learnings/` is NOT a git repo, any claim that git history preserves deleted "
         "learnings files there was false and has been removed from the codebase")

    def _two(self, env):
        dirs, store = env
        put(dirs, "a.md", raw(("2026-09-30T10:00:00", "s1", [line(self.A, "CLAUDE.md", typ="CORRECTION")])))
        put(dirs, "b.md", raw(("2026-09-30T11:00:00", "s2", [line(self.B, "technical-pref.md", typ="CORRECTION")])))
        dr.ingest(store)
        return dirs, store

    def test_both_entries_stay_and_each_names_the_other_file(self, env):
        dirs, store = self._two(env)
        before = total_entries(store)
        path, n = dr.build_review(store, dirs, today=TODAY)
        text = path.read_text()
        assert n == 2 and total_entries(store) == before
        assert "Same fact, other file: queued for technical-pref.md." in text
        assert "Same fact, other file: queued for CLAUDE.md." in text

    def test_the_note_does_not_change_what_a_yes_applies(self, env):
        dirs, store = self._two(env)
        (dirs.memory / "CLAUDE.md").write_text("# c\n")
        path, _ = dr.build_review(store, dirs, today=TODAY)
        items = dr.parse_review(path.read_text())
        assert {i.target for i in items} == {"CLAUDE.md", "technical-pref.md"}
        assert all(i.add.startswith("- ") for i in items)

    def test_an_entry_already_applied_elsewhere_is_named(self, env):
        dirs, store = self._two(env)
        e = store.queue("CLAUDE.md")[0]
        store.move(e.key, store.queue_path("CLAUDE.md"), store.applied_path)
        path, _ = dr.build_review(store, dirs, today=TODAY)
        assert "already applied for CLAUDE.md" in path.read_text()

    def test_unrelated_entries_get_no_note(self, env):
        dirs, store = env
        put(dirs, "a.md", raw(("2026-09-30T10:00:00", "s1", [line(word_salad(1), "me.md"), line(word_salad(2), "project.md")])))
        dr.ingest(store)
        path, _ = dr.build_review(store, dirs, today=TODAY)
        assert "Same fact" not in path.read_text()

    def test_the_same_target_is_not_a_twin(self):
        a = dr.Entry(line="- x", meta={"key": "k1", "target": "me.md"})
        assert not dr.is_twin(a, dr.Entry(line="- x", meta={"key": "k2", "target": "me.md"}))
