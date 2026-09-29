// Tests for the page's pure functions. Run: node tests/logic.test.js
"use strict";

const assert = require("node:assert/strict");
const path = require("node:path");
const L = require(path.join(__dirname, "..", "review_viewer", "static", "logic.js"));

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("replies group by reply_to in file order", () => {
  const threads = L.groupReplies([
    { reply_to: "q1", text: "Looking at it…", done: false },
    { reply_to: "q2", text: "Short answer.", done: true },
    { reply_to: "q1", text: "It is safe.", done: true },
  ]);
  assert.deepEqual([...threads.keys()], ["q1", "q2"]);
  assert.equal(threads.get("q1").text, "Looking at it…\n\nIt is safe.");
  assert.equal(threads.get("q1").done, true);
});

test("split chunks ending on a newline join without a gap", () => {
  const threads = L.groupReplies([
    { reply_to: "q", text: "line 1\nline 2\n", done: false },
    { reply_to: "q", text: "line 3", done: true },
  ]);
  assert.equal(threads.get("q").text, "line 1\nline 2\nline 3");
});

test("spinner runs until a done part arrives, and restarts on a later --more", () => {
  const rows = [{ reply_to: "q", text: "wait", done: false }];
  assert.equal(L.isPending("q", L.groupReplies(rows)), true);
  assert.equal(L.isPending("other", L.groupReplies(rows)), true);
  rows.push({ reply_to: "q", text: "done", done: true });
  assert.equal(L.isPending("q", L.groupReplies(rows)), false);
  rows.push({ reply_to: "q", text: "one more thing", done: false });
  assert.equal(L.isPending("q", L.groupReplies(rows)), true);
});

test("malformed rows are ignored", () => {
  const threads = L.groupReplies([null, { text: "x" }, { reply_to: "q", done: true }]);
  assert.equal(threads.size, 1);
  assert.equal(threads.get("q").text, "");
});

test("poll delay", () => {
  assert.equal(L.pollDelay(1), 2000);
  assert.equal(L.pollDelay(0), 10000);
});

test("citations map to row indices, skipping deleted rows", () => {
  const rows = [
    { k: "ctx", n: 7 }, { k: "ctx", n: 8 }, { k: "del", o: 5, n: null },
    { k: "add", n: 9 }, { k: "gap", n: null }, { k: "ctx", n: 10 },
  ];
  assert.deepEqual(L.citationRows(rows, 8, 9), [1, 3]);
  assert.deepEqual(L.citationRows(rows, 20, 30), []);
});

test("findings group by severity and hide refuted/rejected", () => {
  const fs = [
    { id: "a", severity: "LOW", status: "confirmed" },
    { id: "b", severity: "HIGH", status: "confirmed" },
    { id: "c", severity: "HIGH", status: "refuted" },
    { id: "d", severity: "MEDIUM", status: "unverifiable" },
  ];
  const decisions = { d: { decision: "reject" } };
  const shown = L.groupFindings(fs, decisions, false);
  assert.equal(shown.hidden, 2);
  assert.deepEqual(L.findingOrder(shown), ["b", "a"]);
  const all = L.groupFindings(fs, decisions, true);
  assert.deepEqual(L.findingOrder(all), ["b", "c", "d", "a"]);
});

test("j/k stepping clamps at both ends", () => {
  const order = ["a", "b", "c"];
  assert.equal(L.stepFinding(order, "a", 1), "b");
  assert.equal(L.stepFinding(order, "c", 1), "c");
  assert.equal(L.stepFinding(order, "a", -1), "a");
  assert.equal(L.stepFinding(order, null, 1), "a");
  assert.equal(L.stepFinding(order, null, -1), "c");
  assert.equal(L.stepFinding([], null, 1), null);
});

test("anchor labels", () => {
  assert.equal(L.anchorLabel({ path: "a.py", line_start: 3, line_end: 3 }), "a.py:3");
  assert.equal(L.anchorLabel({ path: "a.py", line_start: 3, line_end: 5 }), "a.py:3–5");
  assert.deepEqual(L.lineRange(9, 4), [4, 9]);
});

test("escapeHtml escapes markup", () => {
  assert.equal(L.escapeHtml(`<img src=x onerror="a">&'`),
    "&lt;img src=x onerror=&quot;a&quot;&gt;&amp;&#39;");
});

test("highlighted HTML splits into balanced lines", () => {
  const html = 'x = <span class="s">"""a\nb"""</span> <span class="c">#c</span>\ny';
  const lines = L.splitHighlighted(html);
  assert.deepEqual(lines, [
    'x = <span class="s">"""a</span>',
    '<span class="s">b"""</span> <span class="c">#c</span>',
    "y",
  ]);
});

test("overlapping polls never append the same rows twice", () => {
  const rows = [{ reply_to: "q", text: "a", done: false }, { reply_to: "q", text: "b", done: true }];
  let cur = { rows: [], since: 0 };
  // Two polls were sent with since=0; both come back with the same rows.
  const first = L.applyPoll(cur, 0, { answers: rows, n: 2 });
  cur = { rows: first.rows, since: first.since };
  const second = L.applyPoll(cur, 0, { answers: rows, n: 2 });
  assert.equal(second.changed, false);
  assert.equal(second.rows.length, 2);
  assert.equal(L.groupReplies(second.rows).get("q").text, "a\n\nb");
});

test("a shrunken outbox resets the read position", () => {
  const next = L.applyPoll({ rows: [{}, {}, {}], since: 3 }, 3, { answers: [], n: 1 });
  assert.deepEqual([next.rows.length, next.since, next.reset], [0, 0, true]);
});

// --- walkthrough -------------------------------------------------------------

const WALK = {
  complete: false,
  steps: [
    { id: "purpose", anchors: [{ path: "a.py", side: "head", line_start: 1, line_end: 2 }], finding_ids: [] },
    { id: "core", anchors: [{ path: "b.py", side: "head", line_start: 5, line_end: 9 }], finding_ids: ["f1"] },
    { id: "tests", anchors: [{ path: "old.py", side: "base", line_start: 3, line_end: 4 }], finding_ids: ["f1", "f2"] },
  ],
  skipped: [{ path: "uv.lock", reason: "generated" }],
};

test("steps keep the walkthrough's order", () => {
  assert.deepEqual(L.stepOrder(WALK), ["purpose", "core", "tests"]);
  assert.deepEqual(L.stepOrder(null), []);
});

test("[ and ] move one step and stop at the ends", () => {
  assert.equal(L.moveStep(WALK, "purpose", 1), "core");
  assert.equal(L.moveStep(WALK, "tests", 1), "tests");
  assert.equal(L.moveStep(WALK, "purpose", -1), "purpose");
  assert.equal(L.moveStep(WALK, null, 1), "purpose");
  assert.equal(L.moveStep(WALK, null, -1), "tests");
  assert.equal(L.moveStep({ steps: [] }, null, 1), null);
  assert.deepEqual(L.stepPosition(WALK, "core"), { index: 2, total: 3 });
});

test("coverage counts anchored or skipped files and linked findings", () => {
  const cov = L.walkCoverage(WALK, ["a.py", "b.py", "c.py", "uv.lock"], [
    { id: "f1", status: "confirmed" }, { id: "f3", status: "unverifiable" },
    { id: "f2", status: "refuted" },
  ]);
  assert.deepEqual(cov, { files: 3, filesTotal: 4, findings: 1, findingsTotal: 2 });
  assert.deepEqual(L.walkCoverage(null, ["a"], []), { files: 0, filesTotal: 1, findings: 0, findingsTotal: 0 });
});

test("anchors map to rows by new or old line number", () => {
  const rows = [
    { k: "ctx", o: null, n: 1 }, { k: "del", o: 3, n: null }, { k: "del", o: 4, n: null },
    { k: "add", o: null, n: 2 }, { k: "gap", o: null, n: null }, { k: "ctx", o: null, n: 3 },
  ];
  assert.deepEqual(L.anchorRows(rows, { side: "head", line_start: 2, line_end: 3 }), [3, 5]);
  assert.deepEqual(L.anchorRows(rows, { side: "base", line_start: 3, line_end: 4 }), [1, 2]);
  assert.equal(L.walkAnchorLabel({ path: "x", side: "base", line_start: 3, line_end: 4 }), "x:3–4 (base)");
});

test("a finding's steps, and the diagram image source", () => {
  assert.deepEqual(L.stepsForFinding(WALK, "f1").map((s) => s.id), ["core", "tests"]);
  assert.deepEqual(L.stepsForFinding(WALK, "zz"), []);
  const url = L.svgDataUrl('<svg xmlns="http://www.w3.org/2000/svg"><text>a & "b"</text></svg>');
  assert.ok(url.startsWith("data:image/svg+xml;charset=utf-8,%3Csvg"));
  assert.ok(!/[<>"&# ]/.test(url.slice(url.indexOf(",") + 1)));
});

test("only github.com links become PR links", () => {
  assert.equal(L.safePrUrl("https://github.com/o/r/pull/7"), "https://github.com/o/r/pull/7");
  assert.equal(L.safePrUrl("javascript:alert(1)"), null);
  assert.equal(L.safePrUrl("https://evil.example/o/r/pull/7"), null);
  assert.equal(L.safePrUrl(undefined), null);
});

test("files group by directory in first-seen order; root files have dir ''", () => {
  const g = L.groupFilesByDir(["a/b.txt", "a/c/d.txt", "a/e.txt", "top.md"]);
  assert.deepEqual(g.map((x) => x.dir), ["a", "a/c", ""]);
  assert.deepEqual(g[0].files, [{ path: "a/b.txt", name: "b.txt" }, { path: "a/e.txt", name: "e.txt" }]);
  assert.deepEqual(g[2].files, [{ path: "top.md", name: "top.md" }]);
  assert.deepEqual(L.groupFilesByDir([]), []);
});

test("a long directory keeps its last two folders after an ellipsis", () => {
  assert.equal(L.shortDir("plugins/multiplai-dev/skills/review-viewer/scripts"), "…/review-viewer/scripts/");
  assert.equal(L.shortDir("plugins/multiplai-dev"), "plugins/multiplai-dev/");
  assert.equal(L.shortDir(".claude-plugin"), ".claude-plugin/");
  assert.equal(L.shortDir(""), "/");
  assert.equal(L.shortDir("a/b/c/d", 3), "…/b/c/d/");
});

test("the sidebar width is clamped, and junk is refused", () => {
  assert.equal(L.clampWidth(100, 160, 700), 160);
  assert.equal(L.clampWidth(900, 160, 700), 700);
  assert.equal(L.clampWidth("321.6", 160, 700), 322);
  assert.equal(L.clampWidth("wide", 160, 700), null);
  assert.equal(L.clampWidth(null, 160, 700), 160);
});

const R = (ks) => ks.split("").map((k, i) => ({ k: { c: "ctx", a: "add", d: "del", g: "gap" }[k], n: i + 1 }));

test("unchanged runs fold, keeping context around changes and kept rows", () => {
  //            0123456789012345
  const rows = R("cccccccccaccccccccccc");
  const items = L.foldRows(rows, [], [], 2, 3);
  assert.deepEqual(items[0], { fold: [0, 6] });
  assert.deepEqual(items.slice(1, 6).map((x) => x.row), [7, 8, 9, 10, 11]);
  assert.deepEqual(items[6], { fold: [12, 20] });
  assert.equal(items.length, 7);
});

test("a short hidden run is shown, not folded; kept and opened rows show", () => {
  const rows = R("ccccacccc");
  assert.ok(L.foldRows(rows, [], [], 2, 3).every((x) => x.row != null));
  const kept = L.foldRows(R("cccccccccccccccc"), [8], [], 1, 3);
  assert.deepEqual(kept.filter((x) => x.row != null).map((x) => x.row), [7, 8, 9]);
  const opened = L.foldRows(R("cccccccccccc"), [], [0, 1], 3, 3);
  assert.deepEqual(opened, [{ row: 0 }, { row: 1 }, { fold: [2, 11] }]);
});

test("gap rows always show; an all-added file has no folds", () => {
  assert.ok(L.foldRows(R("aaaaaaaa"), [], [], 3, 4).every((x) => x.row != null));
  assert.deepEqual(L.foldRows(R("cccccgccccc"), [], [], 3, 4), [{ fold: [0, 4] }, { row: 5 }, { fold: [6, 10] }]);
});

test("expanding a fold opens all of it, or a step from its top or bottom", () => {
  assert.deepEqual(L.expandFold([10, 50], "down", 3), [10, 11, 12]);
  assert.deepEqual(L.expandFold([10, 50], "up", 3), [48, 49, 50]);
  assert.equal(L.expandFold([10, 50], "all", 3).length, 41);
  assert.deepEqual(L.expandFold([10, 11], "down", 20), [10, 11]);
});

test("files move in sidebar order, filtered, stopping at both ends", () => {
  const files = ["a/b.txt", "a/c/d.txt", "a/e.txt", "top.md"];
  assert.deepEqual(L.fileOrder(files, ""), ["a/b.txt", "a/e.txt", "a/c/d.txt", "top.md"]);
  assert.deepEqual(L.fileOrder(files, "TXT"), ["a/b.txt", "a/e.txt", "a/c/d.txt"]);
  const order = L.fileOrder(files, "");
  assert.equal(L.neighbourFile(order, "a/e.txt", 1), "a/c/d.txt");
  assert.equal(L.neighbourFile(order, "a/b.txt", -1), null);
  assert.equal(L.neighbourFile(order, "top.md", 1), null);
  assert.equal(L.neighbourFile(order, "gone.md", 1), null);
});

test("Prev / Next follow the open review step when it spans several files", () => {
  const files = ["a.py", "b.py", "c.py", "d.py"];
  const step = { anchors: [{ path: "c.py" }, { path: "a.py" }, { path: "c.py" }, { path: "gone.py" }] };
  assert.deepEqual(L.navFiles(step, files, "", "a.py"), { order: ["c.py", "a.py"], inStep: true });
  // A file outside the step, or a one-file step: every changed file.
  assert.equal(L.navFiles(step, files, "", "b.py").inStep, false);
  assert.equal(L.navFiles({ anchors: [{ path: "a.py" }] }, files, "", "a.py").inStep, false);
  assert.deepEqual(L.navFiles(null, files, "", "a.py").order, files);
});

test("a file's reviews: steps anchoring it, in order, with the matching anchor", () => {
  const walk = {
    steps: [
      { id: "a", anchors: [{ path: "x.py", line_start: 1, line_end: 2 }] },
      { id: "b", anchors: [{ path: "y.py", line_start: 3, line_end: 4 }, { path: "x.py", line_start: 9, line_end: 9 }] },
    ],
    skipped: [{ path: "uv.lock", reason: "lock file" }],
  };
  const hits = L.stepsForFile(walk, "x.py");
  assert.deepEqual(hits.map((h) => [h.step.id, h.anchor.line_start]), [["a", 1], ["b", 9]]);
  assert.deepEqual(L.stepsForFile(walk, "z.py"), []);
  assert.deepEqual(L.stepsForFile(null, "x.py"), []);
  assert.equal(L.skippedReason(walk, "uv.lock"), "lock file");
  assert.equal(L.skippedReason(walk, "x.py"), null);
  assert.deepEqual([...L.stepFiles(walk.steps[1])], ["y.py", "x.py"]);
  assert.equal(L.stepFiles(null).size, 0);
});

test("summary badges: measured first, then the session's assessments", () => {
  const stats = { badges: [{ id: "size", label: "Small: 10 lines", level: "good", detail: "d" }] };
  const walk = { assessments: [{ topic: "tests", verdict: "concern", title: "Thin tests", detail_md: "**why**" }] };
  const b = L.summaryBadges(stats, walk);
  assert.deepEqual(b.map((x) => [x.source, x.label, x.level]),
    [["measured", "Small: 10 lines", "good"], ["assessed", "Thin tests", "concern"]]);
  assert.notEqual(b[0].key, b[1].key);
  assert.deepEqual(L.summaryBadges(null, null), []);
});

test("clicking a changed row takes its whole contiguous block", () => {
  const rows = [
    { k: "ctx", o: 1, n: 1 }, { k: "del", o: 2, n: null }, { k: "del", o: 3, n: null },
    { k: "add", o: null, n: 2 }, { k: "add", o: null, n: 3 }, { k: "add", o: null, n: 4 },
    { k: "ctx", o: 4, n: 5 }, { k: "del", o: 5, n: null }, { k: "del", o: 6, n: null }, { k: "ctx", o: 7, n: 6 },
  ];
  // A modified block: the head lines of its added rows, from any row in it.
  assert.deepEqual(L.diffBlock(rows, 1), { side: "head", line_start: 2, line_end: 4 });
  assert.deepEqual(L.diffBlock(rows, 5), { side: "head", line_start: 2, line_end: 4 });
  // A block that only deletes: its base lines.
  assert.deepEqual(L.diffBlock(rows, 8), { side: "base", line_start: 5, line_end: 6 });
  assert.equal(L.diffBlock(rows, 0), null);
  assert.equal(L.diffBlock(rows, 99), null);
});

test("references format and parse back, and hand edits are honoured", () => {
  const files = ["a/b.py", "c.md"];
  assert.equal(L.formatRef("a/b.py", { side: "head", line_start: 2, line_end: 4 }), "@a/b.py:2-4");
  assert.equal(L.formatRef("a/b.py", { side: "head", line_start: 7, line_end: 7 }), "@a/b.py:7");
  assert.equal(L.formatRef("a/b.py", { side: "base", line_start: 5, line_end: 6 }), "@a/b.py:base:5-6");
  assert.equal(L.formatRef("c.md", null), "@c.md");
  const refs = L.parseRefs("why @a/b.py:9-3, and @c.md? not @nope.py:1 or me@a/b.py:1 @a/b.py:base:5.", files);
  assert.deepEqual(refs, [
    { path: "a/b.py", side: "head", line_start: 3, line_end: 9 },
    { path: "c.md", side: "head", line_start: null, line_end: null },
    { path: "a/b.py", side: "base", line_start: 5, line_end: 5 },
  ]);
  assert.deepEqual(L.refAnchor(refs), { path: "a/b.py", side: "head", line_start: 3, line_end: 9 });
  assert.equal(L.refAnchor(L.parseRefs("@c.md", files)), null);
  assert.deepEqual(L.parseRefs("@a/b.py:0", files), []);
});

test("@ completion finds the word being typed and ranks file names first", () => {
  assert.deepEqual(L.completion("look at @ser", 12), { start: 8, query: "ser" });
  assert.deepEqual(L.completion("@", 1), { start: 0, query: "" });
  assert.equal(L.completion("mail@x", 6), null);
  assert.equal(L.completion("@a.py:12", 8), null);
  assert.equal(L.completion("@a.py done", 10), null);
  const files = ["app/service.py", "tests/test_service.py", "docs/services.md", "server/app.py"];
  assert.deepEqual(L.matchFiles(files, "serv"), ["app/service.py", "docs/services.md", "tests/test_service.py", "server/app.py"]);
  assert.deepEqual(L.matchFiles(files, "SERVICE"), ["app/service.py", "docs/services.md", "tests/test_service.py"]);
  assert.equal(L.matchFiles(files, "", 2).length, 2);
});

test("each block of changed rows starts once, keyed by its line range", () => {
  const rows = [
    { k: "ctx", o: 1, n: 1 }, { k: "del", o: 2, n: null }, { k: "add", o: null, n: 2 },
    { k: "add", o: null, n: 3 }, { k: "ctx", o: 3, n: 4 }, { k: "del", o: 4, n: null }, { k: "ctx", o: 5, n: 5 },
  ];
  const starts = L.blockStarts(rows);
  assert.deepEqual([...starts.keys()], [1, 5]);
  assert.equal(L.blockKey(starts.get(1)), "head:2-3");
  assert.equal(L.blockKey(starts.get(5)), "base:4-4");
  assert.equal(L.blockStarts([]).size, 0);
});

test("block explanations: the latest light-bulb question per block of a file", () => {
  const a = { path: "x.py", side: "head", line_start: 2, line_end: 3 };
  const qs = [
    { id: "q1", explain: true, anchor: a },
    { id: "q2", explain: false, anchor: a },
    { id: "q3", explain: true, anchor: Object.assign({}, a, { path: "y.py" }) },
    { id: "q4", explain: true, anchor: a },
  ];
  const m = L.explainByBlock(qs, "x.py");
  assert.deepEqual([...m.keys()], ["head:2-3"]);
  assert.equal(m.get("head:2-3").id, "q4");
});

test("the chat holds every question but block explanations, oldest first", () => {
  const qs = [
    { id: "b", ts: "2026-09-27T10:00:05Z" },
    { id: "x", ts: "2026-09-27T10:00:01Z", explain: true },
    { id: "new" },
    { id: "a", ts: "2026-09-27T10:00:01Z" },
  ];
  assert.deepEqual(L.chatQuestions(qs).map((q) => q.id), ["a", "b", "new"]);
});

test("chat status counts answers being written and finished ones not yet seen", () => {
  const qs = [{ id: "q1", ts: "1" }, { id: "q2", ts: "2" }, { id: "q3", ts: "3" }, { id: "q4", ts: "4", explain: true }];
  const replies = new Map([["q1", { text: "done", done: true }], ["q2", { text: "part", done: false }]]);
  // q3 has no reply at all yet: pending too. q4 is a block explanation: not counted.
  assert.deepEqual(L.chatStatus(qs, replies, new Set()), { pending: 2, unread: 1 });
  assert.deepEqual(L.chatStatus(qs, replies, new Set(["q1"])), { pending: 2, unread: 0 });
});

test("adding a reference never repeats one; overlapping ones merge", () => {
  const files = ["a.py", "b.py"];
  const blk = { side: "head", line_start: 10, line_end: 14 };
  let r = L.mergeRef("", files, "a.py", blk);
  assert.equal(r.text, "@a.py:10-14 ");
  // The same block again: unchanged.
  assert.equal(L.mergeRef(r.text, files, "a.py", blk).text, "@a.py:10-14 ");
  // A line inside it: unchanged.
  assert.equal(L.mergeRef("why @a.py:10-14 here", files, "a.py", { side: "head", line_start: 12, line_end: 12 }).text,
    "why @a.py:10-14 here");
  // A line first, then its block: one reference, widened.
  r = L.mergeRef("why @a.py:12?", files, "a.py", blk);
  assert.equal(r.text, "why @a.py:10-14?");
  assert.equal(r.caret, "why @a.py:10-14".length);
  // Two references a new block bridges: merged into one.
  assert.equal(L.mergeRef("@a.py:3 and @a.py:20", files, "a.py", { side: "head", line_start: 4, line_end: 19 }).text,
    "@a.py:3-20 and");
  // Another file, another side, or a gap: added, not merged.
  assert.equal(L.mergeRef("@a.py:3", files, "b.py", blk).text, "@a.py:3 @b.py:10-14 ");
  assert.equal(L.mergeRef("@a.py:12", files, "a.py", { side: "base", line_start: 12, line_end: 12 }).text,
    "@a.py:12 @a.py:base:12 ");
  assert.equal(L.mergeRef("@a.py:1", files, "a.py", { side: "head", line_start: 5, line_end: 6 }, 0).text,
    "@a.py:5-6 @a.py:1");
});

test("reply threads keep the latest part's timestamp", () => {
  const t = L.groupReplies([{ reply_to: "q", text: "a", done: false, ts: "2026-01-01T10:00:00Z" },
    { reply_to: "q", text: "b", done: true, ts: "2026-01-01T10:01:00Z" }]);
  assert.equal(t.get("q").ts, "2026-01-01T10:01:00Z");
});

test("wordDiff marks the changed words of a changed line", () => {
  const d = L.wordDiff("const x = foo(a, b);", "const x = bar(a, c);");
  assert.deepEqual(d.del, [[10, 13], [17, 18]]);
  assert.deepEqual(d.add, [[10, 13], [17, 18]]);
  // Changed words with only a space between them become one mark.
  assert.deepEqual(L.wordDiff("return old value;", "return new thing;").add, [[7, 16]]);
  // A line rewritten from scratch gets no marks: the whole line is the change.
  assert.equal(L.wordDiff("alpha beta gamma", "one two three"), null);
  assert.equal(L.wordDiff("a ".repeat(400), "b ".repeat(400)), null);
});

test("changePairs pairs the i-th deleted row with the i-th added row", () => {
  const rows = [{ k: "ctx" }, { k: "del" }, { k: "del" }, { k: "add" }, { k: "ctx" }, { k: "add" }];
  const p = L.changePairs(rows);
  assert.equal(p.get(1), 3);
  assert.equal(p.get(3), 1);
  assert.equal(p.has(2), false);
  assert.equal(p.has(5), false);
});

test("markRanges wraps text offsets without breaking the highlighter's tags", () => {
  const html = '<span class="k">const</span> x &lt; y';
  assert.equal(L.markRanges(html, [[4, 9]], "wd"),
    '<span class="k">cons<mark class="wd">t</mark></span><mark class="wd"> x &lt;</mark> y');
  assert.equal(L.markRanges(html, [], "wd"), html);
});

test("splitLines pairs deletions left with additions right", () => {
  const rows = [{ k: "ctx" }, { k: "del" }, { k: "del" }, { k: "add" }, { k: "ctx" }, { k: "gap" }, { k: "add" }];
  const items = rows.map((_, i) => ({ row: i }));
  items.splice(4, 1, { fold: [4, 4] });
  assert.deepEqual(L.splitLines(rows, items), [
    { left: 0, right: 0 }, { left: 1, right: 3 }, { left: 2, right: null },
    { fold: [4, 4] }, { gap: 5 }, { left: null, right: 6 },
  ]);
});

test("oldNumbers fills base line numbers on unchanged rows", () => {
  const rows = [{ k: "ctx", n: 1 }, { k: "add", n: 2 }, { k: "ctx", n: 3 }, { k: "del", o: 3 },
    { k: "del", o: 4 }, { k: "ctx", n: 4 }, { k: "gap", t: "…" }, { k: "ctx", n: 40 }];
  assert.deepEqual(L.oldNumbers(rows), [1, null, 2, 3, 4, 5, null, null]);
});

test("enclosingScope finds the nearest opening line above", () => {
  const rows = [{ k: "ctx", t: "class A:" }, { k: "ctx", t: "    def run(self):" },
    { k: "ctx", t: "        x = 1" }, { k: "del", t: "def gone():" }, { k: "add", t: "        y = 2" }];
  assert.equal(L.enclosingScope(rows, 4), "def run(self):");
  assert.equal(L.enclosingScope(rows, 0), "class A:");
  assert.equal(L.enclosingScope([{ k: "ctx", t: "const go = async () => {" }], 0), "const go = async () => {");
  assert.equal(L.enclosingScope([{ k: "ctx", t: "x = 1" }], 0), null);
});

test("stepBlock moves between blocks of changes", () => {
  const starts = [5, 20, 40];
  assert.equal(L.stepBlock(starts, 0, 1), 0);
  assert.equal(L.stepBlock(starts, 5, 1), 1);
  assert.equal(L.stepBlock(starts, 25, 1), 2);
  assert.equal(L.stepBlock(starts, 40, 1), -1);
  assert.equal(L.stepBlock(starts, 25, -1), 1);
  assert.equal(L.stepBlock(starts, 20, -1), 0);
  assert.equal(L.stepBlock(starts, 3, -1), -1);
  assert.equal(L.stepBlock([], 3, 1), -1);
});

test("paletteMatch ranks prefix, then substring, then scattered letters", () => {
  const e = [{ label: "server.py", sub: "review_viewer" }, { label: "observer.js", sub: "static" },
    { label: "setup_rv.sh", sub: "scripts" }, { label: "app.js", sub: "static/server" }];
  assert.deepEqual(L.paletteMatch(e, "serv").map((x) => x.label), ["server.py", "observer.js", "setup_rv.sh", "app.js"]);
  // Scattered letters: the tighter span ranks first.
  assert.deepEqual(L.paletteMatch(e, "srv").map((x) => x.label).slice(0, 3), ["server.py", "observer.js", "setup_rv.sh"]);
  assert.equal(L.paletteMatch(e, "").length, 4);
  assert.deepEqual(L.paletteMatch(e, "zzz"), []);
});

test("viewedCount counts only files in the change", () => {
  assert.deepEqual(L.viewedCount(["a", "b", "c"], { a: "t", gone: "t" }), { done: 1, total: 3 });
  assert.deepEqual(L.viewedCount(["a"], null), { done: 0, total: 1 });
});

let failed = 0;
for (const [name, fn] of tests) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    failed += 1;
    console.log(`not ok - ${name}\n${err.stack}`);
  }
}
console.log(`${tests.length - failed}/${tests.length} passed`);
process.exit(failed ? 1 : 0);
