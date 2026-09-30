/* Pure functions behind the review-viewer page.
 *
 * No DOM, no network: everything here takes data and returns data, so
 * `node tests/logic.test.js` can check it without a browser. In the page these
 * functions are available as `ReviewLogic.<name>`.
 */
(function (root) {
  "use strict";

  const SEVERITIES = ["HIGH", "MEDIUM", "LOW"];
  const HIDDEN_STATUSES = new Set(["refuted", "rejected"]);

  /* Join the parts of one answer. Parts split by `reply` to fit the row size
   * limit end on a line break and are joined as they are; separate replies
   * (a `--more` part, then the rest) get a blank line between them. */
  function joinParts(parts) {
    let out = "";
    for (const part of parts) {
      if (out && !out.endsWith("\n")) out += "\n\n";
      out += part;
    }
    return out;
  }

  /* Outbox rows, in file order → Map(reply_to → {parts, text, done}).
   * A thread is done when its latest part says so: a `--more` part keeps the
   * spinner running until a `done: true` part arrives. */
  function groupReplies(rows) {
    const threads = new Map();
    for (const row of rows || []) {
      if (!row || typeof row.reply_to !== "string") continue;
      let t = threads.get(row.reply_to);
      if (!t) {
        t = { parts: [], text: "", done: false };
        threads.set(row.reply_to, t);
      }
      t.parts.push(String(row.text == null ? "" : row.text));
      t.text = joinParts(t.parts);
      t.done = row.done === true;
      if (typeof row.ts === "string") t.ts = row.ts;
    }
    return threads;
  }

  /* Should the spinner for question `id` still run? */
  function isPending(id, threads) {
    const t = threads.get(id);
    return !t || !t.done;
  }

  /* Fold one /api/poll response into what the page already holds.
   * `current` is {rows, since}; `askedSince` is the `since` the request was
   * sent with. A response to an older request is ignored, so two overlapping
   * polls can never append the same rows twice. Returns the new {rows, since,
   * changed, reset}; `reset` means the outbox shrank and must be read again
   * from the start. */
  function applyPoll(current, askedSince, res) {
    if (askedSince !== current.since) return { rows: current.rows, since: current.since, changed: false, reset: false };
    if (res.n < askedSince) return { rows: [], since: 0, changed: true, reset: true };
    const answers = res.answers || [];
    return {
      rows: answers.length ? current.rows.concat(answers) : current.rows,
      since: res.n,
      changed: answers.length > 0,
      reset: false,
    };
  }

  /* Milliseconds until the next poll. */
  function pollDelay(pendingCount) {
    return pendingCount > 0 ? 2000 : 10000;
  }

  /* Indices of the file-view rows showing head lines start..end. */
  function citationRows(rows, start, end) {
    const out = [];
    (rows || []).forEach(function (row, i) {
      if (row.n != null && row.n >= start && row.n <= end) out.push(i);
    });
    return out;
  }

  function isHidden(finding, decisions) {
    const d = decisions && decisions[finding.id];
    return HIDDEN_STATUSES.has(finding.status) || !!(d && d.decision === "reject");
  }

  /* Findings by severity, in the input order within each severity. Refuted
   * and rejected findings are left out unless showHidden. */
  function groupFindings(findings, decisions, showHidden) {
    const groups = { HIGH: [], MEDIUM: [], LOW: [] };
    let hidden = 0;
    for (const f of findings || []) {
      if (isHidden(f, decisions)) {
        hidden += 1;
        if (!showHidden) continue;
      }
      (groups[f.severity] || (groups[f.severity] = [])).push(f);
    }
    return { groups: groups, hidden: hidden };
  }

  /* Ids in the order the sidebar shows them. */
  function findingOrder(grouped) {
    const ids = [];
    for (const sev of SEVERITIES) for (const f of grouped.groups[sev] || []) ids.push(f.id);
    return ids;
  }

  /* The id `delta` steps from `current`, clamped to the list (j/k). */
  function stepFinding(order, current, delta) {
    if (!order.length) return null;
    const i = order.indexOf(current);
    if (i < 0) return delta > 0 ? order[0] : order[order.length - 1];
    return order[Math.max(0, Math.min(order.length - 1, i + delta))];
  }

  function anchorLabel(anchor) {
    if (!anchor) return "";
    const lines = anchor.line_start === anchor.line_end
      ? String(anchor.line_start) : anchor.line_start + "–" + anchor.line_end;
    return anchor.path + ":" + lines;
  }

  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  /* Split highlighter HTML (spans only) into one string per source line,
   * closing the spans still open at each line end and reopening them on the
   * next line, so a multi-line string or comment stays coloured row by row. */
  function splitHighlighted(html) {
    const lines = [];
    const open = [];
    let current = "";
    const re = /(<span[^>]*>)|(<\/span>)|(\n)|([^<\n]+|<)/g;
    for (const m of String(html).matchAll(re)) {
      if (m[1]) { open.push(m[1]); current += m[1]; }
      else if (m[2]) { open.pop(); current += m[2]; }
      else if (m[3]) {
        lines.push(current + "</span>".repeat(open.length));
        current = open.join("");
      } else current += m[4];
    }
    lines.push(current + "</span>".repeat(open.length));
    return lines;
  }

  /* Line range covered by two row numbers picked in either order. */
  function lineRange(a, b) {
    return a <= b ? [a, b] : [b, a];
  }

  // --- walkthrough ------------------------------------------------------------

  /* Step ids in the order the walkthrough lists them: that order is the
   * session's reading order (purpose, core change, callers, tests, config). */
  function stepOrder(walk) {
    return ((walk && walk.steps) || []).map(function (s) { return s.id; });
  }

  /* The step id `delta` steps from `current`, clamped ([ and ]). With no
   * current step, ] starts at the first and [ at the last. */
  function moveStep(walk, current, delta) {
    return stepFinding(stepOrder(walk), current, delta);
  }

  /* 1-based position of a step and the total, for "Step 2 of 5". */
  function stepPosition(walk, id) {
    const order = stepOrder(walk);
    return { index: order.indexOf(id) + 1, total: order.length };
  }

  /* Changed files an anchor or a skip covers, and findings that must be
   * linked (confirmed, unverifiable) that a step links. */
  function walkCoverage(walk, files, findings) {
    const covered = new Set();
    const linked = new Set();
    for (const s of (walk && walk.steps) || []) {
      for (const a of s.anchors || []) covered.add(a.path);
      for (const id of s.finding_ids || []) linked.add(id);
    }
    for (const k of (walk && walk.skipped) || []) covered.add(k.path);
    const must = (findings || []).filter(function (f) {
      return f.status === "confirmed" || f.status === "unverifiable";
    });
    return {
      files: (files || []).filter(function (f) { return covered.has(f); }).length,
      filesTotal: (files || []).length,
      findings: must.filter(function (f) { return linked.has(f.id); }).length,
      findingsTotal: must.length,
    };
  }

  /* Indices of the file-view rows an anchor covers: head-side anchors match
   * the new line number, base-side ones the old line number. */
  function anchorRows(rows, anchor) {
    const key = anchor && anchor.side === "base" ? "o" : "n";
    const out = [];
    (rows || []).forEach(function (row, i) {
      const v = row[key];
      if (v != null && v >= anchor.line_start && v <= anchor.line_end) out.push(i);
    });
    return out;
  }

  /* "path:3–5" plus " (base)" for a base-side anchor. */
  function walkAnchorLabel(anchor) {
    return anchorLabel(anchor) + (anchor.side === "base" ? " (base)" : "");
  }

  /* Steps that link a finding, in walkthrough order. */
  function stepsForFinding(walk, id) {
    return ((walk && walk.steps) || []).filter(function (s) {
      return (s.finding_ids || []).indexOf(id) >= 0;
    });
  }

  /* An SVG document as an <img> source. The page's CSP allows `data:` images
   * and no inline styles, so a diagram is shown as an image, never inlined. */
  function svgDataUrl(svg) {
    return "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
  }

  /* Changed files grouped by directory, in the order each directory first
   * appears: [{dir, files: [{path, name}]}]. A root file has dir "". */
  function groupFilesByDir(paths) {
    const groups = new Map();
    for (const path of paths || []) {
      const cut = path.lastIndexOf("/");
      const dir = cut < 0 ? "" : path.slice(0, cut);
      if (!groups.has(dir)) groups.set(dir, { dir: dir, files: [] });
      groups.get(dir).files.push({ path: path, name: path.slice(cut + 1) });
    }
    return [...groups.values()];
  }

  /* A directory as its last `keep` folders after "…/", for the file list's
   * group headings; the full directory goes in the tooltip. */
  function shortDir(dir, keep) {
    if (!dir) return "/";
    const parts = dir.split("/");
    const n = keep || 2;
    return parts.length <= n ? dir + "/" : "…/" + parts.slice(-n).join("/") + "/";
  }

  /* The sidebar width in px, kept between `min` and `max` (resize handle). */
  function clampWidth(px, min, max) {
    const n = Number(px);
    if (!Number.isFinite(n)) return null;
    return Math.round(Math.max(min, Math.min(max, n)));
  }

  /* Which rows of a file view to show, and where to fold the rest, as
   * GitHub does. Changed rows (add, del) and the rows in `keep` show with
   * `context` unchanged rows around them; rows in `open` (expanded by the
   * user) show as they are; server gap rows always show. A hidden run
   * shorter than `minFold` is shown instead of folded, since a fold row
   * would take as much room. Returns [{row: i}] and [{fold: [from, to]}]. */
  function foldRows(rows, keep, open, context, minFold) {
    const n = (rows || []).length;
    const show = new Array(n).fill(false);
    const pad = function (i) {
      for (let j = Math.max(0, i - context); j <= Math.min(n - 1, i + context); j++) show[j] = true;
    };
    rows.forEach(function (r, i) {
      if (r.k === "add" || r.k === "del") pad(i);
      else if (r.k === "gap") show[i] = true;
    });
    for (const i of keep || []) if (i >= 0 && i < n) pad(i);
    for (const i of open || []) if (i >= 0 && i < n) show[i] = true;
    const out = [];
    let i = 0;
    while (i < n) {
      if (show[i]) { out.push({ row: i }); i++; continue; }
      let j = i;
      while (j < n && !show[j]) j++;
      if (j - i < minFold) for (let k = i; k < j; k++) out.push({ row: k });
      else out.push({ fold: [i, j - 1] });
      i = j;
    }
    return out;
  }

  /* Row indices to open when a fold is expanded: all of it, or `count`
   * rows from its top ("down": continuing the block above) or its bottom. */
  function expandFold(fold, how, count) {
    const [from, to] = fold;
    let a = from;
    let b = to;
    if (how === "down") b = Math.min(to, from + count - 1);
    else if (how === "up") a = Math.max(from, to - count + 1);
    const out = [];
    for (let i = a; i <= b; i++) out.push(i);
    return out;
  }

  /* The changed files in the order the sidebar lists them (grouped by
   * directory, filtered): the order Prev / Next move through. */
  function fileOrder(files, filter) {
    const f = (filter || "").toLowerCase();
    const shown = (files || []).filter(function (p) { return !f || p.toLowerCase().includes(f); });
    const out = [];
    for (const g of groupFilesByDir(shown)) for (const x of g.files) out.push(x.path);
    return out;
  }

  /* The file `delta` places from `current` in `order`, or null at either end. */
  function neighbourFile(order, current, delta) {
    const i = order.indexOf(current);
    if (i < 0) return null;
    const j = i + delta;
    return j >= 0 && j < order.length ? order[j] : null;
  }

  /* Steps with an anchor on `path`, in walkthrough order, each with the
   * first such anchor: [{step, anchor}]. */
  function stepsForFile(walk, path) {
    const out = [];
    for (const s of (walk && walk.steps) || []) {
      const a = (s.anchors || []).find(function (x) { return x.path === path; });
      if (a) out.push({ step: s, anchor: a });
    }
    return out;
  }

  /* The reason a walkthrough gives for not explaining `path`, or null. */
  function skippedReason(walk, path) {
    const k = ((walk && walk.skipped) || []).find(function (x) { return x.path === path; });
    return k ? k.reason : null;
  }

  /* Where Prev / Next go from `current`: through the open review step's
   * files, in the order its anchors name them, when it spans more than one
   * file and `current` is one of them; otherwise through every changed file
   * as the sidebar lists them. */
  function navFiles(step, files, filter, current) {
    if (step) {
      const seen = [];
      for (const a of step.anchors || []) {
        if ((files || []).includes(a.path) && !seen.includes(a.path)) seen.push(a.path);
      }
      if (seen.length > 1 && seen.includes(current)) return { order: seen, inStep: true };
    }
    return { order: fileOrder(files, filter), inStep: false };
  }

  /* The files a step's anchors point at. */
  function stepFiles(step) {
    return new Set(((step && step.anchors) || []).map(function (a) { return a.path; }));
  }

  /* Badges for the Summary tab: measured ones from the server, then the
   * session's assessments, each tagged with where it came from. */
  function summaryBadges(stats, walk) {
    const out = [];
    for (const b of (stats && stats.badges) || []) {
      out.push({ key: "m:" + b.id, label: b.label, level: b.level, detail: b.detail, source: "measured" });
    }
    for (const a of (walk && walk.assessments) || []) {
      out.push({ key: "a:" + a.topic + ":" + a.title, label: a.title, level: a.verdict,
        detail: a.detail_md, source: "assessed", topic: a.topic });
    }
    return out;
  }

  // --- the risk of merging -----------------------------------------------------

  const TIER_NAMES = ["docs, tests or tooling", "one feature", "a shared module or interface",
    "auth, money, data, shared infra or deploy config"];

  /* The inputs to riskLevel, gathered from what the page has: the measured
   * stats (size, PR checks, the repo's tier file), the walkthrough (the
   * session's tier and revert judgment, the tests verdict) and the findings
   * with their decisions. Null until the walkthrough has a risk block. A
   * finding counts as open while it is confirmed and not rejected. The repo's
   * tier file can only raise the session's tier, never lower it. */
  function riskInputs(stats, walk, findings, decisions, changed) {
    if (!walk || !walk.risk) return null;
    const repoTiers = (stats && stats.tiers) || {};
    const files = changed || [];
    const matched = files.filter((p) => p in repoTiers).map((p) => repoTiers[p]);
    const fileTier = matched.length ? Math.max.apply(null, matched) : -1;
    const sessionTier = walk.risk.tier;
    const tier = Math.max(fileTier, sessionTier);
    const badge = (id) => ((stats && stats.badges) || []).find((b) => b.id === id);
    const tests = ((walk.assessments || []).find((a) => a.topic === "tests") || {}).verdict || null;
    const open = { HIGH: 0, MEDIUM: 0 };
    for (const f of findings || []) {
      const d = decisions && decisions[f.id];
      if (f.status === "confirmed" && !(d && d.decision === "reject") && f.severity in open) open[f.severity] += 1;
    }
    const checks = badge("checks");
    const size = badge("size");
    return {
      tier: tier,
      tierWhy: fileTier > sessionTier ? "set by the repo's .review-risk.toml" : walk.risk.tier_why,
      revertable: walk.risk.revertable,
      revertWhy: walk.risk.revert_why,
      tests: tests,
      checksFailing: !!checks && checks.level === "concern",
      large: !!size && size.level === "concern",
      openHigh: open.HIGH,
      openMedium: open.MEDIUM,
    };
  }

  /* Low, Medium or High, and the rules that put it there. The first rule
   * list that has any rule true wins; every true rule in it is a reason. */
  function riskLevel(r) {
    const n = (k, word) => k + " open " + word + " finding" + (k === 1 ? "" : "s");
    const tierName = "tier " + r.tier + " (" + TIER_NAMES[r.tier] + ")";
    const high = [
      [r.openHigh > 0, n(r.openHigh, "HIGH")],
      [r.tier === 3 && !r.revertable, tierName + " that a revert cannot undo"],
      [r.tier === 3 && r.tests === "concern", tierName + " without tests"],
      [r.checksFailing, "PR checks failing"],
    ];
    const medium = [
      [r.tier === 3, tierName],
      [r.tier === 2 && r.tests !== "good", tierName + (r.tests ? " with tests: " + r.tests : " with no tests verdict")],
      [r.openMedium > 0, n(r.openMedium, "MEDIUM")],
      [r.large, "a large diff"],
      [!r.revertable, "a revert cannot undo it"],
    ];
    for (const [level, rules] of [["high", high], ["medium", medium]]) {
      const why = rules.filter((x) => x[0]).map((x) => x[1]);
      if (why.length) return { level: level, reasons: why };
    }
    return { level: "low", reasons: ["no rule for Medium or High applies"] };
  }

  // --- @ references in questions ------------------------------------------------

  /* The contiguous run of added and deleted rows around row `ri`, as a line
   * range: the head line numbers of its added rows, or, for a run that only
   * deletes, the base line numbers of its deleted rows. Null on other rows. */
  function diffBlock(rows, ri) {
    const changed = function (r) { return !!r && (r.k === "add" || r.k === "del"); };
    if (!changed(rows[ri])) return null;
    let a = ri;
    let b = ri;
    while (changed(rows[a - 1])) a--;
    while (changed(rows[b + 1])) b++;
    const block = rows.slice(a, b + 1);
    const adds = block.filter(function (r) { return r.k === "add" && r.n != null; }).map(function (r) { return r.n; });
    const nums = adds.length ? adds : block.map(function (r) { return r.o; }).filter(function (v) { return v != null; });
    if (!nums.length) return null;
    return { side: adds.length ? "head" : "base", line_start: Math.min.apply(null, nums), line_end: Math.max.apply(null, nums) };
  }

  /* The first row of every block of changed rows, mapped to the block's
   * line range (as diffBlock gives it): Map(rowIndex -> {side, line_start, line_end}). */
  function blockStarts(rows) {
    const out = new Map();
    (rows || []).forEach(function (r, i) {
      const changed = r.k === "add" || r.k === "del";
      const prev = rows[i - 1];
      if (changed && !(prev && (prev.k === "add" || prev.k === "del"))) {
        const b = diffBlock(rows, i);
        if (b) out.set(i, b);
      }
    });
    return out;
  }

  /* One key per block of one file: "side:start-end". */
  function blockKey(range) {
    return (range.side || "head") + ":" + range.line_start + "-" + range.line_end;
  }

  /* The latest light-bulb question for each block of `path`: Map(blockKey -> question). */
  function explainByBlock(questions, path) {
    const out = new Map();
    for (const q of questions || []) {
      if (q.explain && q.anchor && q.anchor.path === path) out.set(blockKey(q.anchor), q);
    }
    return out;
  }

  /* The chat: every question except block explanations (those live in
   * strips above their blocks), oldest first. A question without a
   * timestamp (just sent) sorts last. */
  function chatQuestions(questions) {
    return (questions || []).filter(function (q) { return !q.explain; })
      .map(function (q, i) { return [q, i]; })
      .sort(function (x, y) {
        const a = x[0].ts || "\uffff";
        const b = y[0].ts || "\uffff";
        return a < b ? -1 : a > b ? 1 : x[1] - y[1];
      })
      .map(function (x) { return x[0]; });
  }

  /* For the collapsed chat line: how many answers are still being written,
   * and how many finished answers the reader has not opened the chat to see. */
  function chatStatus(questions, replies, seen) {
    let pending = 0;
    let unread = 0;
    for (const q of chatQuestions(questions)) {
      if (isPending(q.id, replies)) pending += 1;
      else if (replies && replies.has(q.id) && !(seen && seen.has(q.id))) unread += 1;
    }
    return { pending: pending, unread: unread };
  }

  /* "@path", "@path:12", "@path:12-20", "@path:base:40-52". */
  function formatRef(path, ref) {
    let out = "@" + path;
    if (!ref || ref.line_start == null) return out;
    if (ref.side === "base") out += ":base";
    out += ":" + ref.line_start;
    if (ref.line_end != null && ref.line_end !== ref.line_start) out += "-" + ref.line_end;
    return out;
  }

  /* Every @reference in `text` that names a changed file, in order:
   * [{path, side, line_start, line_end}], lines null for a bare file. A
   * range written backwards is put in order; trailing punctuation is
   * ignored, so "see @a.py:3." works. */
  function parseRefs(text, files) {
    const known = new Set(files || []);
    const out = [];
    const re = /(^|\s)@([^\s@]+)/g;
    let m;
    while ((m = re.exec(text || "")) !== null) {
      const token = m[2].replace(/[.,;:!?)\]]+$/, "");
      const parts = /^(.*?)(:base)?(?::(\d+)(?:-(\d+))?)?$/.exec(token);
      if (!parts || !known.has(parts[1])) continue;
      let a = parts[3] != null ? Number(parts[3]) : null;
      let b = parts[4] != null ? Number(parts[4]) : a;
      if (a != null && (a < 1 || b < 1)) continue;
      if (a != null && b < a) { const t = a; a = b; b = t; }
      out.push({ path: parts[1], side: parts[2] ? "base" : "head", line_start: a, line_end: b });
    }
    return out;
  }

  /* Where each @reference to a changed file sits in `text`:
   * [{start, end, ref}], `end` exclusive, trailing punctuation left out. */
  function refSpans(text, files) {
    const known = new Set(files || []);
    const out = [];
    const re = /(^|\s)@([^\s@]+)/g;
    let m;
    while ((m = re.exec(text || "")) !== null) {
      const token = m[2].replace(/[.,;:!?)\]]+$/, "");
      const parts = /^(.*?)(:base)?(?::(\d+)(?:-(\d+))?)?$/.exec(token);
      if (!parts || !known.has(parts[1])) continue;
      const start = m.index + m[1].length;
      let a = parts[3] != null ? Number(parts[3]) : null;
      let b = parts[4] != null ? Number(parts[4]) : a;
      if (a != null && b < a) { const t = a; a = b; b = t; }
      out.push({ start: start, end: start + 1 + token.length,
        ref: { path: parts[1], side: parts[2] ? "base" : "head", line_start: a, line_end: b } });
    }
    return out;
  }

  /* Add a reference to `text` without repeating one already there. A
   * reference to the same file and side whose lines overlap or touch the new
   * ones is widened to cover both (a line, then its whole block, leaves one
   * reference); one that already covers the new lines is left alone.
   * Otherwise the reference goes at `at`, spaced from its neighbours.
   * Returns {text, caret}. */
  function mergeRef(text, files, path, ref, at) {
    const spans = refSpans(text, files).filter(function (s) {
      const r = s.ref;
      return r.path === path && r.side === (ref.side || "head") && r.line_start != null &&
        r.line_start <= ref.line_end + 1 && ref.line_start <= r.line_end + 1;
    });
    if (spans.length) {
      let lo = ref.line_start;
      let hi = ref.line_end;
      for (const s of spans) { lo = Math.min(lo, s.ref.line_start); hi = Math.max(hi, s.ref.line_end); }
      const token = formatRef(path, { side: ref.side, line_start: lo, line_end: hi });
      let out = text;
      // Rewrite the first overlapping reference; drop the rest (right to left).
      for (let i = spans.length - 1; i >= 1; i--) {
        const s = spans[i];
        out = out.slice(0, s.start).replace(/[ \t]+$/, "") + out.slice(s.end);
      }
      out = out.slice(0, spans[0].start) + token + out.slice(spans[0].end);
      return { text: out, caret: spans[0].start + token.length };
    }
    const pos = at == null ? text.length : at;
    const before = text.slice(0, pos);
    const after = text.slice(pos);
    const lead = before && !/\s$/.test(before) ? " " : "";
    const trail = after && /^\s/.test(after) ? "" : " ";
    const token = formatRef(path, ref);
    return { text: before + lead + token + trail + after, caret: (before + lead + token + trail).length };
  }

  /* The anchor a question is sent with: its first reference that has lines. */
  function refAnchor(refs) {
    const r = (refs || []).find(function (x) { return x.line_start != null; });
    return r ? { path: r.path, side: r.side, line_start: r.line_start, line_end: r.line_end } : null;
  }

  /* The @word being typed just before the caret, for autocomplete:
   * {start, query} (start is the index of "@"), or null. */
  function completion(text, caret) {
    const m = /(^|\s)@([^\s@:]*)$/.exec((text || "").slice(0, caret));
    return m ? { start: caret - m[2].length - 1, query: m[2] } : null;
  }

  /* Changed files matching `query`, best first: file names that start with
   * it, then file names that contain it, then paths that contain it. */
  function matchFiles(files, query, limit) {
    const q = (query || "").toLowerCase();
    const scored = [];
    for (const path of files || []) {
      const lower = path.toLowerCase();
      const name = lower.slice(lower.lastIndexOf("/") + 1);
      const rank = !q ? 2 : name.startsWith(q) ? 0 : name.includes(q) ? 1 : lower.includes(q) ? 2 : -1;
      if (rank >= 0) scored.push([rank, path]);
    }
    scored.sort(function (x, y) { return x[0] - y[0]; });
    return scored.slice(0, limit || 8).map(function (x) { return x[1]; });
  }

  // --- word-level changes ----------------------------------------------------------

  /* Words, runs of spaces, and single punctuation marks. */
  function tokens(text) {
    return String(text).match(/[A-Za-z0-9_]+|\s+|[^A-Za-z0-9_\s]/g) || [];
  }

  /* Which characters differ between a deleted line and the added line that
   * replaces it: {del: [[start, end]], add: [[start, end]]}, ends exclusive,
   * found by a longest-common-subsequence over tokens. Null when the lines
   * share too little for the marks to help (the whole line changed), or are
   * too long to compare cheaply. */
  function wordDiff(a, b, opts) {
    const o = opts || {};
    const maxTokens = o.maxTokens || 300;
    const minShared = o.minShared == null ? 0.4 : o.minShared;
    const x = tokens(a);
    const y = tokens(b);
    if (x.length > maxTokens || y.length > maxTokens) return null;
    const n = x.length;
    const m = y.length;
    const dp = [];
    for (let i = 0; i <= n; i++) dp.push(new Array(m + 1).fill(0));
    for (let i = n - 1; i >= 0; i--) {
      for (let j = m - 1; j >= 0; j--) {
        dp[i][j] = x[i] === y[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
      }
    }
    const keepX = new Array(n).fill(false);
    const keepY = new Array(m).fill(false);
    let i = 0;
    let j = 0;
    while (i < n && j < m) {
      if (x[i] === y[j]) { keepX[i] = true; keepY[j] = true; i++; j++; }
      else if (dp[i + 1][j] >= dp[i][j + 1]) i++;
      else j++;
    }
    const sharedChars = x.reduce(function (acc, t, k) { return acc + (keepX[k] ? t.length : 0); }, 0);
    const longest = Math.max(String(a).length, String(b).length);
    if (!longest || sharedChars / longest < minShared) return null;
    // Changed tokens as character ranges; two changes with only spaces
    // between them become one mark.
    const ranges = function (toks, keep) {
      const out = [];
      const text = toks.join("");
      let pos = 0;
      toks.forEach(function (t, k) {
        const start = pos;
        pos += t.length;
        if (keep[k] || /^\s+$/.test(t)) return;
        const last = out[out.length - 1];
        const between = last ? text.slice(last[1], start) : null;
        if (last && /^\s*$/.test(between)) last[1] = pos;
        else out.push([start, pos]);
      });
      return out;
    };
    return { del: ranges(x, keepX), add: ranges(y, keepY) };
  }

  /* In each block of changed rows, the i-th deleted row paired with the i-th
   * added row: Map(rowIndex -> partner rowIndex), both directions. */
  function changePairs(rows) {
    const out = new Map();
    let i = 0;
    const n = (rows || []).length;
    while (i < n) {
      const k = rows[i].k;
      if (k !== "add" && k !== "del") { i++; continue; }
      const dels = [];
      const adds = [];
      while (i < n && (rows[i].k === "add" || rows[i].k === "del")) {
        (rows[i].k === "del" ? dels : adds).push(i);
        i++;
      }
      for (let p = 0; p < Math.min(dels.length, adds.length); p++) {
        out.set(dels[p], adds[p]);
        out.set(adds[p], dels[p]);
      }
    }
    return out;
  }

  /* Wrap the characters in `ranges` (text offsets, ends exclusive) of
   * highlighter HTML in <mark class="cls">, closing and reopening the mark
   * around the highlighter's own tags so the result stays well nested.
   * An entity such as &lt; counts as one character. */
  function markRanges(html, ranges, cls) {
    if (!ranges || !ranges.length) return html;
    const inRange = function (pos) {
      return ranges.some(function (r) { return pos >= r[0] && pos < r[1]; });
    };
    const open = '<mark class="' + cls + '">';
    let out = "";
    let pos = 0;
    let marking = false;
    const re = /(<[^>]+>)|(&[#A-Za-z0-9]+;)|([^<&])/g;
    for (const m of String(html).matchAll(re)) {
      if (m[1]) {
        if (marking) { out += "</mark>"; marking = false; }
        out += m[1];
        continue;
      }
      const want = inRange(pos);
      if (want && !marking) { out += open; marking = true; }
      else if (!want && marking) { out += "</mark>"; marking = false; }
      out += m[2] || m[3];
      pos += 1;
    }
    if (marking) out += "</mark>";
    return out;
  }

  /* Side-by-side lines from foldRows items: an unchanged row fills both
   * sides; in a block of changes the deleted rows go left and the added rows
   * right, paired in order; folds and server gaps span both.
   * [{left, right}] (row indices or null), [{fold}], [{gap}]. */
  function splitLines(rows, items) {
    const out = [];
    let dels = [];
    let adds = [];
    const flush = function () {
      for (let p = 0; p < Math.max(dels.length, adds.length); p++) {
        out.push({ left: p < dels.length ? dels[p] : null, right: p < adds.length ? adds[p] : null });
      }
      dels = [];
      adds = [];
    };
    for (const it of items || []) {
      if (it.fold) { flush(); out.push({ fold: it.fold }); continue; }
      const r = rows[it.row];
      if (r.k === "del") { if (adds.length) flush(); dels.push(it.row); }
      else if (r.k === "add") adds.push(it.row);
      else { flush(); out.push(r.k === "gap" ? { gap: it.row } : { left: it.row, right: it.row }); }
    }
    flush();
    return out;
  }

  /* Base-side line numbers for every row: the server gives them only on
   * deleted rows, and an unchanged row's is its head number minus the lines
   * added and plus the lines deleted above it. A gap resets the count from
   * the next row that carries both numbers. Returns an array like rows. */
  function oldNumbers(rows) {
    let delta = 0;
    let known = true;
    return (rows || []).map(function (r) {
      if (r.k === "gap") { known = false; return null; }
      if (r.o != null) {
        if (r.n != null) { delta = r.n - r.o; known = true; }
        if (r.k === "del") delta -= 1;
        return r.o;
      }
      if (r.k === "add") { delta += 1; return null; }
      return known && r.n != null ? r.n - delta : null;
    });
  }

  /* The nearest line at or above row `ri` that opens a function, class or
   * similar scope, trimmed, for the code pane's header; null if none. */
  const SCOPE_RE = new RegExp("^\\s*(?:export\\s+)?(?:default\\s+)?(?:pub(?:\\([^)]*\\))?\\s+)?" +
    "(?:async\\s+)?(?:(?:def|class|function|func|fn|impl|interface|struct|enum|trait|module)\\b|" +
    "(?:const|let|var)\\s+[A-Za-z_$][\\w$]*\\s*=\\s*(?:async\\s*)?(?:function\\b|\\([^)]*\\)\\s*=>))");
  function enclosingScope(rows, ri) {
    for (let i = Math.min(ri, (rows || []).length - 1); i >= 0; i--) {
      const r = rows[i];
      if (r.k === "gap" || r.k === "del") continue;
      if (SCOPE_RE.test(r.t)) {
        const t = r.t.trim();
        return t.length > 80 ? t.slice(0, 79) + "…" : t;
      }
    }
    return null;
  }

  /* The index in `starts` (sorted row indices of block starts) of the block
   * `delta` away from the one containing row `ri`; -1 when there is none. */
  function stepBlock(starts, ri, delta) {
    if (!starts.length) return -1;
    let cur = -1;
    for (let i = 0; i < starts.length; i++) if (starts[i] <= ri) cur = i;
    const next = delta > 0 ? (ri < starts[0] ? 0 : cur + 1) : (cur < 0 ? -1 : (starts[cur] < ri ? cur : cur - 1));
    return next >= 0 && next < starts.length ? next : -1;
  }

  /* The index in `starts` of the current block of changes. It is the block at
   * row `ri` (the reference line), unless the pane cannot scroll far enough to
   * bring the block last stepped to (starting at row `pinRow`) up to that line:
   * `edge` says which ends the pane is against ({ top, bottom }), and a pinned
   * block below the line at the bottom, or above it at the top, wins. */
  function currentBlock(starts, ri, pinRow, edge) {
    let pos = -1;
    if (ri != null) for (let i = 0; i < starts.length; i++) if (starts[i] <= ri) pos = i;
    const pin = pinRow == null ? -1 : starts.indexOf(pinRow);
    if (pin < 0 || !edge) return pos;
    if ((edge.bottom && pin > pos) || (edge.top && pin < pos)) return pin;
    return pos;
  }

  /* The index in `starts` of the block `delta` away from the current one
   * (see currentBlock); -1 when there is none. */
  function stepCurrent(starts, ri, pinRow, edge, delta) {
    const cur = currentBlock(starts, ri, pinRow, edge);
    const pos = currentBlock(starts, ri, null, null);
    if (cur === pos) {
      if (ri == null) return -1;
      const idx = stepBlock(starts, ri, delta);
      // At the top of the pane, the start of the current block is above the
      // reference line and already on screen: stepping back to it cannot
      // scroll, so step to the block before it (or to none).
      if (delta < 0 && edge && edge.top && idx === pos && idx >= 0) return pos > 0 ? pos - 1 : -1;
      return idx;
    }
    const next = cur + delta;
    return next >= 0 && next < starts.length ? next : -1;
  }

  // --- the Go to palette -------------------------------------------------------------

  /* Entries whose text matches `query` as a subsequence, best first:
   * a match at the start of the label, then one in the label, then one in
   * its sub-line; tighter matches rank higher. An empty query keeps the
   * input order. */
  function paletteMatch(entries, query, limit) {
    const q = (query || "").trim().toLowerCase();
    const max = limit || 50;
    if (!q) return (entries || []).slice(0, max);
    const span = function (text) {
      const t = text.toLowerCase();
      let first = -1;
      let j = 0;
      for (let i = 0; i < t.length && j < q.length; i++) {
        if (t[i] === q[j]) { if (first < 0) first = i; j++; if (j === q.length) return { first: first, len: i - first + 1 }; }
      }
      return null;
    };
    const scored = [];
    (entries || []).forEach(function (e, idx) {
      const label = String(e.label || "");
      const sub = String(e.sub || "");
      const lower = label.toLowerCase();
      let score = null;
      if (lower.startsWith(q)) score = 0;
      else if (lower.includes(q)) score = 10 + lower.indexOf(q) / 100;
      else {
        const a = span(label);
        if (a) score = 20 + (a.len - q.length) + a.first / 100;
        else {
          const b = span(label + " " + sub);
          if (b) score = 200 + (b.len - q.length);
        }
      }
      if (score != null) scored.push([score, idx, e]);
    });
    scored.sort(function (x, y) { return x[0] - y[0] || x[1] - y[1]; });
    return scored.slice(0, max).map(function (x) { return x[2]; });
  }

  /* "3 of 12 viewed", counting only files still in the change. */
  function viewedCount(files, viewed) {
    const v = viewed || {};
    return { done: (files || []).filter(function (f) { return Object.prototype.hasOwnProperty.call(v, f); }).length,
      total: (files || []).length };
  }

  /* Only GitHub PR links get an <a> in the header. */
  function safePrUrl(url) {
    return typeof url === "string" && /^https:\/\/github\.com\/[^\s"'<>]+$/.test(url) ? url : null;
  }

  const api = {
    SEVERITIES: SEVERITIES, joinParts: joinParts, groupReplies: groupReplies,
    isPending: isPending, pollDelay: pollDelay, applyPoll: applyPoll, citationRows: citationRows,
    isHidden: isHidden, groupFindings: groupFindings, findingOrder: findingOrder,
    stepFinding: stepFinding, anchorLabel: anchorLabel, escapeHtml: escapeHtml,
    splitHighlighted: splitHighlighted, lineRange: lineRange,
    stepOrder: stepOrder, moveStep: moveStep, stepPosition: stepPosition,
    walkCoverage: walkCoverage, anchorRows: anchorRows,
    walkAnchorLabel: walkAnchorLabel, stepsForFinding: stepsForFinding,
    svgDataUrl: svgDataUrl, safePrUrl: safePrUrl,
    groupFilesByDir: groupFilesByDir, shortDir: shortDir, clampWidth: clampWidth,
    foldRows: foldRows, expandFold: expandFold, fileOrder: fileOrder,
    neighbourFile: neighbourFile, navFiles: navFiles,
    stepsForFile: stepsForFile, skippedReason: skippedReason, stepFiles: stepFiles,
    summaryBadges: summaryBadges,
    diffBlock: diffBlock, formatRef: formatRef, parseRefs: parseRefs, refAnchor: refAnchor,
    completion: completion, matchFiles: matchFiles,
    blockStarts: blockStarts, blockKey: blockKey, explainByBlock: explainByBlock,
    chatQuestions: chatQuestions, chatStatus: chatStatus, refSpans: refSpans, mergeRef: mergeRef,
    tokens: tokens, wordDiff: wordDiff, changePairs: changePairs, markRanges: markRanges,
    splitLines: splitLines, oldNumbers: oldNumbers, enclosingScope: enclosingScope, stepBlock: stepBlock,
    riskInputs: riskInputs, riskLevel: riskLevel, TIER_NAMES: TIER_NAMES,
    currentBlock: currentBlock, stepCurrent: stepCurrent,
    paletteMatch: paletteMatch, viewedCount: viewedCount,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ReviewLogic = api;
})(typeof window !== "undefined" ? window : this);
