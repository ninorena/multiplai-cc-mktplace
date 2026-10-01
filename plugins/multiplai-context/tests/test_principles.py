"""Principles: the model groups learnings, the user ticks one sentence for several.

As in test_daily_review.py, the property that matters most is that no path
loses a learning. Several tests count learnings before and after.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from lib import daily_review as dr
from lib import principles as pr
from test_daily_review import dream_env, env, line, put, raw  # noqa: F401  (fixtures)

TODAY = "2026-10-01"
TOMORROW = "2026-10-02"

CHECK = ["check git log before trusting a summary", "verify the live repo state first",
         "confirm with gh pr list before acting", "read the actual file not the injected note"]
OTHER = ["prefers tables for comparisons", "timezone is pacific"]


def seed(dirs, descs, target="me.md", name="a.md"):
    put(dirs, name, raw(("2026-09-30T10:00:00", "s1", [line(d, target) for d in descs])))


def ids_for(prompt, words):
    """Short ids in *prompt* whose learning contains any of *words*."""
    out = []
    for l in prompt.splitlines():
        m = re.match(r"^(e\d+) \[[^\]]+\] (.*)$", l)
        if m and any(w in m.group(2) for w in words):
            out.append(m.group(1))
    return out


def model_answer(principles=(), support=()):
    """A stand-in model. Each principle is (sentence, words that pick its learnings)."""
    calls = []

    async def ask(prompt):
        calls.append(prompt)
        ps = []
        for sentence, words in principles:
            ids = ids_for(prompt, words)
            ps.append({"sentence": sentence, "entries": ids, "examples": ids[:1]})
        ss = []
        for number, words in support:
            ids = ids_for(prompt, words)
            ss.append({"principle": number, "entries": ids, "examples": ids[:2]})
        return json.dumps({"principles": ps, "support": ss})

    ask.calls = calls
    return ask


def tick_principle(path, heading, what):
    text = path.read_text()
    parts = re.split(r"(?m)^(?=### )", text)
    for i, p in enumerate(parts):
        if p.startswith(f"### {heading}"):
            parts[i] = p.replace(f"- [ ] {what}", f"- [x] {what}", 1)
    path.write_text("".join(parts))


def everywhere(store):
    """Every learning the store holds, by key, wherever it is."""
    keys = []
    for t in store.queue_targets():
        keys += [e.key for e in store.queue(t)]
    for p in (store.reserve_path, store.rejected_path, store.applied_path, pr.rolled_up_path(store)):
        keys += [e.key for e in store.read(p)]
    return keys


@pytest.fixture
def run(dream_env, monkeypatch):
    dream, dirs, store = dream_env
    (dirs.memory / "me.md").write_text("# me\n")

    def go(ask, day=TODAY):
        monkeypatch.setattr(dream, "_ask_for_principles", ask)
        assert dream._daily_review(day) == 0
        return dirs.dreams / f"review-{day}.md"

    return dream, dirs, store, go


CHECK_P = ("Check live state before acting on a summary.", ["check git", "verify the live", "confirm with", "read the actual"])


# ---------------------------------------------------------------- the review file


class TestReviewFile:
    def test_principle_is_shown_above_the_facts_and_its_learnings_are_not_facts(self, run):
        dream, dirs, store, go = run
        seed(dirs, CHECK + OTHER)
        path = go(model_answer([CHECK_P]))
        text = path.read_text()
        assert text.index("## Principles") < text.index("## Facts")
        assert "### Principle A" in text and CHECK_P[0] in text
        assert "Behind it: 4 learning(s), for me.md." in text
        facts = dr.parse_review(text)
        assert len(facts) == 1
        shown = text.split("## Facts", 1)[1]
        assert not any(c in shown for c in CHECK)

    def test_a_model_failure_leaves_a_facts_only_review(self, run, capsys):
        dream, dirs, store, go = run
        seed(dirs, CHECK + OTHER)

        async def broken(prompt):
            raise RuntimeError("no network")

        path = go(broken)
        text = path.read_text()
        assert "## Principles" not in text and "## Facts" in text
        assert "Principles step skipped (RuntimeError" in capsys.readouterr().out

    def test_a_garbled_answer_also_leaves_facts_only(self, run):
        dream, dirs, store, go = run
        seed(dirs, CHECK + OTHER)

        async def garbled(prompt):
            return "I think the principle is to check things."

        assert "## Principles" not in go(garbled).read_text()

    def test_no_model_call_when_too_few_learnings(self, run):
        dream, dirs, store, go = run
        seed(dirs, OTHER)
        ask = model_answer()
        go(ask)
        assert ask.calls == []

    def test_a_later_principle_comes_back_without_a_new_call(self, run):
        dream, dirs, store, go = run
        seed(dirs, CHECK + OTHER)
        go(model_answer([CHECK_P]))
        tick_principle(dirs.dreams / f"review-{TODAY}.md", "Principle A", "later")
        dream._daily_apply(None, TODAY)
        seed(dirs, ["uses tmux windows per agent", "likes short replies", "works in pacific time"], name="b.md")
        ask = model_answer()
        path = go(ask, TOMORROW)
        assert CHECK_P[0] in path.read_text()
        # One waiting principle leaves room for 2 more, so the model is asked,
        # but it is not shown the learnings already behind the waiting one.
        assert len(ask.calls) == 1 and ids_for(ask.calls[0], ["tmux"]) and not ids_for(ask.calls[0], CHECK)


# ---------------------------------------------------------------- ticks


class TestApply:
    def test_yes_writes_the_book_the_examples_and_rolls_up_every_learning(self, run):
        dream, dirs, store, go = run
        seed(dirs, CHECK + OTHER)
        path = go(model_answer([CHECK_P]))
        before = sorted(everywhere(store))
        tick_principle(path, "Principle A", "yes")
        assert dream._daily_apply(None, TODAY) == 0
        book = (dirs.memory / pr.BOOK).read_text()
        assert f"- P1. {CHECK_P[0]}" in book and "> Cap: 40 principles" in book
        ex = (dirs.memory / pr.EXAMPLES).read_text()
        assert "## P1. " in ex and ex.count("\n- ") == 1   # examples=ids[:1]
        rolled = store.read(pr.rolled_up_path(store))
        assert len(rolled) == 4 and all(e.meta["principle"] == "P1" for e in rolled)
        assert sorted(everywhere(store)) == before
        assert "Result: added as P1 on 2026-10-01" in path.read_text()
        assert pr.load_pending(store) == []

    def test_an_edited_sentence_is_what_goes_in_the_book(self, run):
        dream, dirs, store, go = run
        seed(dirs, CHECK)
        path = go(model_answer([CHECK_P]))
        path.write_text(path.read_text().replace(CHECK_P[0], "Trust the repo, not the recap."))
        tick_principle(path, "Principle A", "yes")
        dream._daily_apply(None, TODAY)
        assert "- P1. Trust the repo, not the recap." in (dirs.memory / pr.BOOK).read_text()

    def test_no_keeps_the_learnings_and_tells_the_model_next_time(self, run):
        dream, dirs, store, go = run
        seed(dirs, CHECK + OTHER)
        path = go(model_answer([CHECK_P]))
        before = sorted(everywhere(store))
        tick_principle(path, "Principle A", "no")
        dream._daily_apply(None, TODAY)
        assert sorted(everywhere(store)) == before
        assert len(store.queue("me.md")) == 6
        assert CHECK_P[0] in pr.rejected_principles_path(store).read_text()
        ask = model_answer()
        path2 = go(ask, TOMORROW)
        assert CHECK_P[0] in ask.calls[0].split("## Learnings")[0]
        # Back as single facts: one per file per day.
        assert len(dr.parse_review(path2.read_text())) == 1

    def test_a_full_book_refuses_and_changes_nothing(self, run):
        dream, dirs, store, go = run
        (dirs.memory / pr.BOOK).write_text("# Principles\n\n" + "".join(f"- P{i}. rule {i}\n" for i in range(1, 41)))
        seed(dirs, CHECK)
        path = go(model_answer([CHECK_P]))
        tick_principle(path, "Principle A", "yes")
        dream._daily_apply(None, TODAY)
        assert CHECK_P[0] not in (dirs.memory / pr.BOOK).read_text()
        assert len(store.queue("me.md")) == 4 and len(pr.load_pending(store)) == 1

    def test_two_ticks_leave_it_waiting(self, run):
        dream, dirs, store, go = run
        seed(dirs, CHECK)
        path = go(model_answer([CHECK_P]))
        tick_principle(path, "Principle A", "yes")
        tick_principle(path, "Principle A", "no")
        dream._daily_apply(None, TODAY)
        assert not (dirs.memory / pr.BOOK).exists() and len(pr.load_pending(store)) == 1

    def test_a_second_apply_changes_nothing(self, run):
        dream, dirs, store, go = run
        seed(dirs, CHECK)
        path = go(model_answer([CHECK_P]))
        tick_principle(path, "Principle A", "yes")
        dream._daily_apply(None, TODAY)
        snap = [(p, p.read_text()) for p in (dirs.memory / pr.BOOK, dirs.memory / pr.EXAMPLES, path)]
        dream._daily_apply(None, TODAY)
        assert all(p.read_text() == t for p, t in snap)

    def test_principles_and_facts_apply_in_one_run(self, run):
        dream, dirs, store, go = run
        seed(dirs, CHECK + OTHER)
        path = go(model_answer([CHECK_P]))
        tick_principle(path, "Principle A", "yes")
        text = path.read_text()
        path.write_text(text[:text.index("## Facts")] + text[text.index("## Facts"):].replace("- [ ] yes", "- [x] yes", 1))
        dream._daily_apply(None, TODAY)
        assert (dirs.memory / pr.BOOK).exists()
        assert len(store.read(store.applied_path)) == 1


class TestSupport:
    def _book_with_p1(self, dirs):
        (dirs.memory / pr.BOOK).write_text("# Principles\n\n- P1. Check live state before acting on a summary.\n")

    def test_yes_adds_examples_under_the_existing_number(self, run):
        dream, dirs, store, go = run
        self._book_with_p1(dirs)
        seed(dirs, CHECK[:2] + OTHER)
        path = go(model_answer(support=[("P1", ["check git", "verify the live"])]))
        assert "### More examples for P1" in path.read_text()
        tick_principle(path, "More examples for P1", "yes")
        dream._daily_apply(None, TODAY)
        assert (dirs.memory / pr.BOOK).read_text().count("- P") == 1
        ex = (dirs.memory / pr.EXAMPLES).read_text()
        assert ex.count("## P1.") == 1 and ex.count("\n- ") == 2
        assert len(store.read(pr.rolled_up_path(store))) == 2

    def test_no_is_remembered_so_the_same_pair_is_not_offered_again(self, run):
        dream, dirs, store, go = run
        self._book_with_p1(dirs)
        seed(dirs, CHECK[:2] + OTHER)
        ask = model_answer(support=[("P1", ["check git", "verify the live"])])
        path = go(ask)
        tick_principle(path, "More examples for P1", "no")
        dream._daily_apply(None, TODAY)
        path2 = go(ask, TOMORROW)
        assert "More examples for P1" not in path2.read_text()

    def test_support_for_a_number_not_in_the_book_is_dropped(self, run):
        dream, dirs, store, go = run
        seed(dirs, CHECK[:2] + OTHER)
        path = go(model_answer(support=[("P9", ["check git"])]))
        assert "More examples" not in path.read_text()


# ---------------------------------------------------------------- reading the answer


class TestParse:
    def _idmap(self, n):
        return {f"e{i}": dr.Entry(line=line(f"word{i} thing{i}"), meta={"key": f"k{i}", "target": "me.md"})
                for i in range(1, n + 1)}

    def parse(self, obj, n=9, **kw):
        args = dict(book_numbers=set(), declined={}, max_new=3, today=TODAY) | kw
        return pr.parse_candidates(json.dumps(obj), self._idmap(n), **args)

    def test_fewer_than_three_learnings_is_not_a_principle(self):
        assert self.parse({"principles": [{"sentence": "x", "entries": ["e1", "e2"]}]}) == []

    def test_unknown_ids_do_not_count(self):
        assert self.parse({"principles": [{"sentence": "x", "entries": ["e1", "e2", "e99"]}]}) == []

    def test_a_learning_sits_behind_one_principle_only(self):
        out = self.parse({"principles": [{"sentence": "a", "entries": ["e1", "e2", "e3"]},
                                         {"sentence": "b", "entries": ["e3", "e4", "e5"]}]})
        assert [c.sentence for c in out] == ["a"]

    def test_at_most_max_new(self):
        ps = [{"sentence": f"s{i}", "entries": [f"e{3*i+1}", f"e{3*i+2}", f"e{3*i+3}"]} for i in range(3)]
        assert len(self.parse({"principles": ps}, max_new=2)) == 2

    def test_examples_fall_back_to_the_first_learnings(self):
        out = self.parse({"principles": [{"sentence": "a", "entries": ["e1", "e2", "e3"], "examples": ["e7"]}]})
        assert out[0].examples == ["k1", "k2"]

    def test_text_around_the_json_is_ignored(self):
        text = 'Here you go:\n{"principles": [{"sentence": "a", "entries": ["e1","e2","e3"]}]}\nDone.'
        assert len(pr.parse_candidates(text, self._idmap(3), book_numbers=set(), declined={},
                                       max_new=3, today=TODAY)) == 1

    def test_no_json_raises(self):
        with pytest.raises(ValueError):
            pr.parse_candidates("nothing here", {}, book_numbers=set(), declined={}, max_new=3, today=TODAY)


# ---------------------------------------------------------------- guards


def test_the_router_skips_both_principles_files_in_both_places():
    scripts = Path(pr.__file__).resolve().parent.parent
    want = {"claude.md"} | {n.lower() for n in pr.NOT_ROUTED}
    for f, name in (("generators/memory.py", "_NOT_ROUTED"), ("context_manager.py", "_NOT_ROUTED_MEMORY")):
        src = (scripts / f).read_text()
        m = re.search(rf"^{name} = frozenset\(\{{(.*?)\}}\)", src, re.M)
        assert m, f
        assert set(re.findall(r'"([^"]+)"', m.group(1))) == want, f


def test_the_module_never_deletes():
    src = Path(pr.__file__).read_text()
    assert "unlink" not in src and "rmtree" not in src and "os.remove" not in src
