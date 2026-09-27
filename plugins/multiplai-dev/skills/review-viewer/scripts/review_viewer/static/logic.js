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
    return HIDDEN_STATUSES.has(finding.status) || (d && d.decision === "reject");
  }

  /* Findings by severity, in the input order within each severity. Refuted,
   * rejected, and findings the user rejected are left out unless showHidden. */
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

  /* What the walkthrough tab says about its state. */
  function walkStatus(walk) {
    if (!walk) return "Waiting for the walkthrough";
    if (!walk.complete) return "Walkthrough in progress";
    return "";
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
   * directory, filtered), which is the order scrolling moves through. */
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

  /* Scrolling past the top or bottom of a file moves to the previous or next
   * file, but only on a fresh push: wheel events that arrive while the pane is
   * already at its edge count only after a pause of `pauseMs`, so the momentum
   * of a fling that reached the edge never turns the page. Returns the new
   * accumulator and whether to move. `edge` is -1 (top), 1 (bottom) or 0. */
  function overscroll(acc, edge, delta, now, opts) {
    const o = opts || {};
    const pauseMs = o.pauseMs == null ? 200 : o.pauseMs;
    const need = o.need == null ? 300 : o.need;
    const dir = delta > 0 ? 1 : delta < 0 ? -1 : 0;
    const last = acc ? acc.last : -Infinity;
    if (!dir || edge !== dir) return { move: false, acc: { armed: false, total: 0, dir: 0, last: now } };
    let armed = acc && acc.armed && acc.dir === dir;
    let total = armed ? acc.total : 0;
    if (!armed && now - last >= pauseMs) armed = true;
    if (armed) total += Math.abs(delta);
    if (armed && total >= need) return { move: true, acc: { armed: false, total: 0, dir: 0, last: now } };
    return { move: false, acc: { armed: armed, total: total, dir: dir, last: now }, progress: armed ? total / need : 0 };
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
    walkCoverage: walkCoverage, walkStatus: walkStatus, anchorRows: anchorRows,
    walkAnchorLabel: walkAnchorLabel, stepsForFinding: stepsForFinding,
    svgDataUrl: svgDataUrl, safePrUrl: safePrUrl,
    groupFilesByDir: groupFilesByDir, shortDir: shortDir, clampWidth: clampWidth,
    foldRows: foldRows, expandFold: expandFold, fileOrder: fileOrder,
    neighbourFile: neighbourFile, overscroll: overscroll,
    stepsForFile: stepsForFile, skippedReason: skippedReason, stepFiles: stepFiles,
    summaryBadges: summaryBadges,
    diffBlock: diffBlock, formatRef: formatRef, parseRefs: parseRefs, refAnchor: refAnchor,
    completion: completion, matchFiles: matchFiles,
    blockStarts: blockStarts, blockKey: blockKey, explainByBlock: explainByBlock,
    chatQuestions: chatQuestions, chatStatus: chatStatus, refSpans: refSpans, mergeRef: mergeRef,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ReviewLogic = api;
})(typeof window !== "undefined" ? window : this);
