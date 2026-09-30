"""Spent learnings files are moved to ``learnings/archived/``, never deleted.

``.multiplai/learnings/`` is not a git repo, so a deleted learnings file is gone
for good. The skill used to say "Git history preserves whatever does get
collected", which was false. These tests pin the replacement: the collector
and the ``--auto`` apply move files, and every failure path leaves the file
where it was.
"""

from __future__ import annotations

import os
from pathlib import Path

from test_mark_processed_batch import (  # noqa: F401  (gc_env is a fixture)
    LEARNINGS_A,
    LEARNINGS_B,
    _archive,
    _ledger_all,
    _write_learnings,
    gc_env,
)

PLUGIN = Path(__file__).parent.parent
SKILL = (PLUGIN / "skills" / "dream-remember" / "SKILL.md").read_text()
DREAM_SRC = (PLUGIN / "scripts" / "dream.py").read_text()


def _ready(env, name="2026-07-29.md", text=LEARNINGS_A):
    _write_learnings(env, name, text)
    _ledger_all(env, name, "p.md")
    _archive(env, "p.md")


class TestGcMovesInsteadOfDeleting:
    def test_file_moves_to_archived_with_content_intact(self, gc_env, capsys):
        _ready(gc_env)

        gc_env["dream"]._gc_learnings()

        assert not (gc_env["learnings"] / "2026-07-29.md").exists()
        assert (gc_env["learnings"] / "archived" / "2026-07-29.md").read_text() == LEARNINGS_A
        assert "GC learnings: archived 1, kept 0" in capsys.readouterr().out

    def test_an_existing_archived_name_is_never_overwritten(self, gc_env):
        archived = gc_env["learnings"] / "archived"
        archived.mkdir()
        (archived / "2026-07-29.md").write_text("OLDER COPY\n")
        _ready(gc_env)

        gc_env["dream"]._gc_learnings()

        assert (archived / "2026-07-29.md").read_text() == "OLDER COPY\n"
        assert (archived / "2026-07-29-2.md").read_text() == LEARNINGS_A

    def test_a_failed_move_keeps_the_file_and_says_so(self, gc_env, monkeypatch, capsys):
        _ready(gc_env)

        def boom(*a, **k):
            raise PermissionError("nope")

        monkeypatch.setattr(os, "rename", boom)

        gc_env["dream"]._gc_learnings()

        assert (gc_env["learnings"] / "2026-07-29.md").read_text() == LEARNINGS_A
        out = capsys.readouterr().out
        assert "could not archive" in out
        assert "archived 0, kept 1" in out

    def test_only_the_decided_file_moves(self, gc_env):
        _ready(gc_env)
        _write_learnings(gc_env, "2026-07-30.md", LEARNINGS_B)  # not ledgered

        gc_env["dream"]._gc_learnings()

        assert (gc_env["learnings"] / "archived" / "2026-07-29.md").exists()
        assert (gc_env["learnings"] / "2026-07-30.md").read_text() == LEARNINGS_B
        assert not (gc_env["learnings"] / "archived" / "2026-07-30.md").exists()

    def test_archived_files_are_not_picked_up_again(self, gc_env, capsys):
        _ready(gc_env)
        gc_env["dream"]._gc_learnings()
        capsys.readouterr()

        blocks, files = gc_env["dream"]._collect_blocks(gc_env["learnings"])
        assert blocks == [] and files == []

        gc_env["dream"]._gc_learnings()
        assert "no learnings files" in capsys.readouterr().out
        assert (gc_env["learnings"] / "archived" / "2026-07-29.md").exists()

    def test_the_ledger_is_pruned_for_moved_files(self, gc_env):
        _ready(gc_env)

        gc_env["dream"]._gc_learnings()

        assert gc_env["ledger"].load(gc_env["ledger_path"])["processed"] == {}


class TestArchiveHelper:
    def test_moves_and_returns_the_destination(self, tmp_path):
        import dream

        f = tmp_path / "x.md"
        f.write_text("keep me\n")
        dest = dream._archive_learning(f)
        assert dest == tmp_path / "archived" / "x.md"
        assert dest.read_text() == "keep me\n"
        assert not f.exists()

    def test_a_missing_source_raises_and_creates_nothing_to_lose(self, tmp_path):
        import dream

        try:
            dream._archive_learning(tmp_path / "gone.md")
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("expected FileNotFoundError")


class TestNothingDeletesALearningsFile:
    def test_auto_apply_archives_its_sources(self):
        assert "Deleted processed learnings" not in DREAM_SRC
        auto = DREAM_SRC.split("failed_count == 0:")[1][:600]
        assert "_archive_learning(f)" in auto
        assert ".unlink(" not in auto

    def test_no_unlink_of_a_learnings_file_remains_in_the_gc_or_auto_path(self):
        gc = DREAM_SRC.split("def _gc_learnings")[1].split("\ndef ")[0]
        assert ".unlink(" not in gc

    def test_skill_no_longer_claims_git_history_keeps_them(self):
        assert "Git history preserves" not in SKILL
        assert "not a git repo" in SKILL

    def test_skill_step_five_names_the_archive_folder(self):
        step5 = SKILL.split("## Step 5")[1].split("## Step 6")[0]
        assert "archived/" in step5
