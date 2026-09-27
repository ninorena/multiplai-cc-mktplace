/* review-viewer page: findings beside the code, questions to the live session.
 *
 * Every /api call carries the token boot.js took from the address bar.
 * Text written by a person or a model is only ever set with textContent, or
 * with innerHTML after DOMPurify has sanitised the rendered markdown. Code is
 * set with innerHTML only as highlighter output or escaped text.
 */
(function () {
  "use strict";

  const L = window.ReviewLogic;
  const $ = (id) => document.getElementById(id);
  const NOT_AUTHORISED = "This page is not authorised. Open the link the viewer printed " +
    "(the \"open: file://…\" line) again.";
  const SERVER_GONE = "The viewer server is not answering. It stops after 30 minutes " +
    "without an open page; ask the session to start it again.";

  const state = {
    token: typeof REVIEW_TOKEN === "string" ? REVIEW_TOKEN : "",
    who: null,
    targets: [],
    slug: null,
    detail: null,
    findingsById: new Map(),
    selected: null,
    filePath: null,
    view: null,
    views: new Map(),
    openRows: new Map(),
    shownRows: new Set(),
    overscroll: null,
    showHidden: false,
    fileFilter: "",
    questions: [],
    replyRows: [],
    replies: new Map(),
    since: 0,
    pick: null,
    pollTimer: null,
    polling: null,
    pollAgain: false,
    pollGen: 0,
    tab: "summary",
    fileNote: null,
    ac: null,
    chatOpen: false,
    quietFocus: false,
    chatJustOpened: false,
    seen: new Set(),
    badgeOpen: null,
    tabChosen: false,
    walk: null,
    walkKey: "",
    stepId: null,
    walkFocus: null,
  };

  // --- small helpers ---------------------------------------------------------

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const c of [].concat(children || [])) {
      if (c == null) continue;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return node;
  }

  function showBanner(text) {
    const b = $("banner");
    b.textContent = text;
    b.hidden = !text;
  }

  async function api(path, body) {
    const opts = { headers: { "X-Review-Token": state.token }, cache: "no-store" };
    if (body !== undefined) {
      opts.method = "POST";
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(path, opts);
    } catch (err) {
      showBanner(SERVER_GONE);
      throw err;
    }
    if (res.status === 401) {
      showBanner(NOT_AUTHORISED);
      throw new Error("unauthorised");
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "HTTP " + res.status);
    if ($("banner").textContent === SERVER_GONE) showBanner("");
    return data;
  }

  function targetUrl(suffix) {
    return "/api/targets/" + encodeURIComponent(state.slug) + (suffix || "");
  }

  function highlight(text, language) {
    const hljs = window.hljs;
    if (hljs && language && hljs.getLanguage(language)) {
      try {
        return L.splitHighlighted(hljs.highlight(text, { language: language, ignoreIllegals: true }).value);
      } catch (err) { /* fall through to plain text */ }
    }
    return L.escapeHtml(text).split("\n");
  }

  function renderMarkdown(node, text) {
    if (window.marked && window.DOMPurify) {
      node.innerHTML = window.DOMPurify.sanitize(window.marked.parse(text));
      if (window.hljs) node.querySelectorAll("pre code").forEach((c) => window.hljs.highlightElement(c));
    } else {
      node.classList.add("plain");
      node.textContent = text;
    }
  }

  /* Diagrams. mermaid renders to an SVG string; DOMPurify cleans it (SVG
   * profile); the result is shown as an <img> with a data: URL. The CSP allows
   * `data:` images and no inline styles, so the SVG is never put into the page
   * itself. Any failure shows the diagram source instead. */
  let mermaidReady = false;
  let diagramSeq = 0;
  const diagramCache = new Map();

  function diagramFallback(node, source, why) {
    node.replaceChildren(
      el("p", { class: "muted", text: "Diagram not rendered (" + why + "); its source:" }),
      el("pre", { class: "sketch diagram-fallback", text: source }));
  }

  function sizedSvg(clean) {
    const doc = new DOMParser().parseFromString(clean, "image/svg+xml");
    const svg = doc.documentElement;
    if (!svg || svg.nodeName !== "svg") throw new Error("not an SVG");
    const box = (svg.getAttribute("viewBox") || "").split(/[\s,]+/).map(Number);
    if (box.length === 4 && box[2] > 0 && box[3] > 0) {
      svg.setAttribute("width", String(Math.ceil(box[2])));
      svg.setAttribute("height", String(Math.ceil(box[3])));
    }
    svg.removeAttribute("style");
    if (!svg.getAttribute("xmlns")) svg.setAttribute("xmlns", "http://www.w3.org/2000/svg");
    return new XMLSerializer().serializeToString(svg);
  }

  async function diagramUrl(source) {
    if (diagramCache.has(source)) return diagramCache.get(source);
    const dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    if (!mermaidReady) {
      window.mermaid.initialize({
        startOnLoad: false, securityLevel: "strict", theme: dark ? "dark" : "default",
        htmlLabels: false, flowchart: { htmlLabels: false }, fontFamily: "sans-serif",
      });
      mermaidReady = true;
    }
    const id = "walk-diagram-" + (++diagramSeq);
    try {
      const out = await window.mermaid.render(id, source);
      const clean = window.DOMPurify.sanitize(out.svg, { USE_PROFILES: { svg: true, svgFilters: true } });
      const url = L.svgDataUrl(sizedSvg(clean));
      diagramCache.set(source, url);
      return url;
    } finally {
      // mermaid leaves its scratch element behind when a render fails.
      for (const stray of [document.getElementById(id), document.getElementById("d" + id)]) {
        if (stray) stray.remove();
      }
    }
  }

  async function renderDiagram(node, diagram) {
    const source = diagram.source;
    if (!window.mermaid || !window.DOMPurify) {
      diagramFallback(node, source, "mermaid did not load");
      return;
    }
    try {
      const url = await diagramUrl(source);
      const img = el("img", { class: "diagram", alt: "Diagram for this step", src: url });
      node.replaceChildren(img);
    } catch (err) {
      diagramFallback(node, source, String((err && err.message) || err).split("\n")[0]);
    }
  }

  // --- loading ---------------------------------------------------------------

  async function boot() {
    if (!state.token) {
      showBanner(NOT_AUTHORISED);
      return;
    }
    try {
      state.who = await api("/api/whoami");
    } catch (err) {
      return;
    }
    const sid = (state.who.session_id || "").slice(0, 8) || "no session id";
    $("who").textContent = "Answers come from " + state.who.agent + " (session " + sid + ")";
    const res = await api("/api/targets");
    state.targets = res.targets;
    const sel = $("target-select");
    if (state.targets.length > 1) {
      sel.hidden = false;
      for (const t of state.targets) sel.appendChild(el("option", { value: t.slug, text: t.label }));
      sel.addEventListener("change", () => loadTarget(sel.value));
    }
    bindEvents();
    setInterval(() => api("/api/alive").catch(() => {}), 10000);
    if (state.targets.length) await loadTarget(state.targets[0].slug);
    schedulePoll();
  }

  async function loadTarget(slug) {
    state.pollGen += 1;
    state.slug = slug;
    state.detail = await api(targetUrl());
    state.findingsById = new Map(state.detail.findings.findings.map((f) => [f.id, f]));
    state.questions = state.detail.questions || [];
    state.replyRows = [];
    state.replies = new Map();
    state.since = 0;
    state.views = new Map();
    state.openRows = new Map();
    state.selected = null;
    state.pick = null;
    state.walk = null;
    state.walkKey = "";
    state.stepId = null;
    state.walkFocus = null;
    state.fileNote = null;
    state.badgeOpen = null;
    if (!state.tabChosen) state.tab = "summary";
    const target = state.detail.findings.target;
    $("title").textContent = target.label;
    document.title = target.label + " · review";
    renderHeader();
    renderSidebar();
    renderTabs();
    renderWalk();
    renderSummary();
    await Promise.all([pollOnce(), pollWalk()]);
    const first = L.findingOrder(L.groupFindings(state.detail.findings.findings,
      state.detail.decisions, state.showHidden))[0];
    if (first) await selectFinding(first, { stay: true });
    else {
      renderDetail();
      if (state.detail.files.length) await openFile(state.detail.files[0]);
    }
  }

  // --- header, tabs ----------------------------------------------------------------

  function renderHeader() {
    const pr = state.detail.pr;
    const box = $("pr-info");
    box.hidden = !pr;
    if (pr) {
      const url = L.safePrUrl(pr.url);
      box.replaceChildren(
        el("span", { class: "pr-title", text: "PR #" + pr.number + " " + (pr.title || "") }),
        pr.author ? el("span", { class: "muted", text: " by " + pr.author }) : null,
        url ? el("a", { href: url, target: "_blank", rel: "noreferrer noopener", text: " open on GitHub" }) : null);
    }
    const notice = $("notice");
    notice.textContent = state.detail.notice || "";
    notice.hidden = !state.detail.notice;
  }

  function setTab(tab) {
    state.tab = tab;
    state.tabChosen = true;
    renderTabs();
    if (state.view) renderCode();
    renderThread();
  }

  const TABS = { summary: ["tab-summary", "summary"], walk: ["tab-walk", "walkthrough"], finding: ["tab-finding", "finding"] };

  function renderTabs() {
    for (const [name, [tab, panel]] of Object.entries(TABS)) {
      const on = state.tab === name;
      $(tab).setAttribute("aria-selected", String(on));
      $(tab).classList.toggle("active", on);
      $(panel).hidden = !on;
    }
    $("decide").hidden = state.tab !== "finding" || !(state.selected && state.findingsById.get(state.selected));
    const n = state.walk ? state.walk.steps.length : 0;
    $("walk-count").textContent = !state.walk ? "(waiting)" : (state.walk.complete ? "(" + n + ")" : "(" + n + ", in progress)");
    const nf = state.detail ? state.detail.findings.findings.length : 0;
    $("finding-count").textContent = nf ? "(" + nf + ")" : "";
  }

  // --- summary ---------------------------------------------------------------------

  function renderSummary() {
    const agent = state.who ? state.who.agent : "the session";
    const badges = L.summaryBadges(state.detail.stats, state.walk);
    const box = $("badges");
    box.replaceChildren();
    const groups = [["measured", "Measured from git" + (state.detail.pr ? " and GitHub" : "")],
      ["assessed", "Assessed by " + agent]];
    for (const [source, title] of groups) {
      const mine = badges.filter((b) => b.source === source);
      const row = el("div", { class: "badge-row" }, [el("div", { class: "label", text: title })]);
      if (!mine.length) {
        row.appendChild(el("span", { class: "muted small", text: source === "measured"
          ? "Not available: git could not read these commits."
          : (state.walk ? "None yet." : "Written with the walkthrough.") }));
      }
      for (const b of mine) {
        row.appendChild(el("button", {
          class: "qbadge " + b.level + (state.badgeOpen === b.key ? " open" : ""),
          title: source === "measured" ? b.detail : "Click for " + agent + "'s reasoning",
          "aria-expanded": String(state.badgeOpen === b.key),
          text: b.label,
          onclick: () => { state.badgeOpen = state.badgeOpen === b.key ? null : b.key; renderSummary(); },
        }));
      }
      box.appendChild(row);
    }
    const open = badges.find((b) => b.key === state.badgeOpen);
    const detail = $("badge-detail");
    detail.hidden = !open;
    if (open) {
      if (open.source === "assessed") renderMarkdown(detail, open.detail);
      else detail.replaceChildren(el("p", { text: open.detail }));
    }
    $("walk-status").textContent = L.walkStatus(state.walk);
    $("walk-status").hidden = !L.walkStatus(state.walk);
    const overview = $("walk-overview");
    if (!state.walk) {
      overview.replaceChildren(el("p", { class: "muted", text: "The session writes an overview and the reviews after it reads the diff; they appear here as they are written." }));
    } else {
      renderMarkdown(overview, state.walk.overview_md);
      const cov = L.walkCoverage(state.walk, state.detail.files, state.detail.findings.findings);
      overview.appendChild(el("p", { class: "muted small", text: "The reviews cover " + cov.files + " of " + cov.filesTotal +
        " changed files" + (cov.findingsTotal ? " and link " + cov.findings + " of " + cov.findingsTotal + " findings" : "") + "." }));
      if (state.walk.skipped.length) {
        const ul = el("ul", { class: "skipped" });
        for (const k of state.walk.skipped) {
          ul.appendChild(el("li", {}, [el("span", { class: "mono", text: k.path }), " — " + k.reason]));
        }
        overview.appendChild(el("div", { class: "label", text: "Files no review explains, and why" }));
        overview.appendChild(ul);
      }
    }
    const pr = state.detail.pr;
    const desc = $("pr-desc");
    desc.hidden = !pr;
    if (pr) {
      // The PR body is text from GitHub, written by whoever opened the PR:
      // it goes through the same marked + DOMPurify path as everything else.
      if ((pr.body || "").trim()) renderMarkdown($("pr-body"), pr.body);
      else $("pr-body").replaceChildren(el("p", { class: "muted", text: "The PR has no description." }));
    }
  }

  // --- walkthrough -----------------------------------------------------------------

  async function pollWalk() {
    const gen = state.pollGen;
    let walk = null;
    try {
      walk = await api(targetUrl("/walkthrough"));
    } catch (err) {
      if (err.message !== "no walkthrough yet") return;
    }
    if (gen !== state.pollGen) return;
    const key = walk ? JSON.stringify(walk) : "";
    if (key === state.walkKey) return;
    state.walkKey = key;
    state.walk = walk;
    if (state.stepId && !L.stepOrder(walk).includes(state.stepId)) state.stepId = null;
    renderTabs();
    renderWalk();
    renderSummary();
    if (!state.tabChosen && walk && state.tab === "walk" && !state.stepId && walk.steps.length) {
      await selectStep(walk.steps[0].id, { open: !state.filePath || !state.detail.findings.findings.length });
    }
  }

  function currentStep() {
    if (!state.walk || !state.stepId) return null;
    return state.walk.steps.find((s) => s.id === state.stepId) || null;
  }

  function renderWalk() {
    const list = $("walk-steps");
    list.replaceChildren();
    if (!state.walk) {
      list.appendChild(el("li", { class: "muted", text: "Waiting for the session to write the reviews." }));
      $("walk-step").replaceChildren();
      return;
    }
    state.walk.steps.forEach((s, i) => {
      list.appendChild(el("li", {}, [el("button", {
        class: "step-item" + (s.id === state.stepId ? " selected" : ""),
        "data-step": s.id,
        onclick: async () => {
          await selectStep(s.id, { open: true });
          $("walk-step").scrollIntoView({ block: "start", behavior: "smooth" });
        },
      }, [el("span", { class: "step-n", text: String(i + 1) }), el("span", { text: s.title })])]));
    });
    renderStep();
  }

  function renderStep() {
    const box = $("walk-step");
    box.replaceChildren();
    const step = currentStep();
    if (!step) {
      if (state.fileNote) {
        const reason = L.skippedReason(state.walk, state.fileNote);
        box.appendChild(el("p", {}, [el("span", { class: "mono", text: state.fileNote }),
          reason ? " is not explained: " + reason + "." : " is not explained by any review yet."]));
      } else if (state.walk && state.walk.steps.length) {
        box.appendChild(el("p", { class: "muted", text: "Pick a review, click a file, or press ] to start." }));
      }
      return;
    }
    const others = state.fileNote ? L.stepsForFile(state.walk, state.fileNote).filter((x) => x.step.id !== step.id) : [];
    const pos = L.stepPosition(state.walk, step.id);
    box.appendChild(el("div", { class: "step-nav" }, [
      el("button", { text: "◀ Previous", disabled: pos.index <= 1, onclick: () => moveStep(-1) }),
      el("span", { class: "muted", text: "Step " + pos.index + " of " + pos.total }),
      el("button", { text: "Next ▶", disabled: pos.index >= pos.total, onclick: () => moveStep(1) }),
    ]));
    box.appendChild(el("h2", { text: step.title }));
    if (others.length) {
      box.appendChild(el("p", { class: "muted small" }, ["Also about this file: ",
        ...others.map((x, i) => el("button", { class: "cite-link", text: (i ? ", " : "") + x.step.title,
          onclick: () => showStepAt(x.step.id, x.anchor) }))]));
    }
    const anchors = el("div", { class: "step-anchors" });
    for (const a of step.anchors) {
      anchors.appendChild(el("button", {
        class: "cite-link walk-anchor", text: L.walkAnchorLabel(a),
        onclick: () => openAnchor(a),
      }));
    }
    box.appendChild(anchors);
    const body = el("div", { class: "md" });
    renderMarkdown(body, step.body_md);
    box.appendChild(body);
    if (step.diagram) {
      const holder = el("div", { class: "diagram-box" }, [el("p", { class: "muted", text: "Rendering diagram…" })]);
      box.appendChild(holder);
      renderDiagram(holder, step.diagram);
    }
    const cards = step.finding_ids.map((id) => state.findingsById.get(id)).filter(Boolean);
    if (cards.length) {
      box.appendChild(el("div", { class: "label", text: "Findings in this step" }));
      for (const f of cards) {
        box.appendChild(el("button", {
          class: "finding-item finding-card", "data-id": f.id,
          onclick: () => { setTab("finding"); selectFinding(f.id); },
        }, [
          el("span", { class: "badge " + f.severity, text: f.severity }),
          el("span", { class: "badge", text: f.status }),
          el("span", { class: "claim", text: f.claim }),
          el("span", { class: "where", text: f.file + ":" + f.line_start }),
        ]));
      }
    }
  }

  async function selectStep(id, opts) {
    state.stepId = id;
    state.fileNote = null;
    if (state.tab !== "walk") {
      state.tab = "walk";
      renderTabs();
    }
    clearPick();
    renderWalk();
    const step = currentStep();
    if (step && (!opts || opts.open !== false)) await openAnchor(step.anchors[0]);
    else if (state.view) renderCode();
    renderFiles();
    renderThread();
  }

  /* Show a step with one of its anchors (not necessarily the first). */
  async function showStepAt(id, anchor) {
    const note = state.fileNote;
    await selectStep(id, { open: false });
    state.fileNote = note;
    renderWalk();
    await openAnchor(anchor);
    renderFiles();
  }

  /* A file was picked in the sidebar (or scrolled to): show the review that
   * explains it, or say that none does. */
  async function showFileReview(path) {
    const hits = L.stepsForFile(state.walk, path);
    state.fileNote = path;
    if (state.tab !== "walk") {
      state.tab = "walk";
      state.tabChosen = true;
      renderTabs();
    }
    if (hits.length) {
      if (state.stepId !== hits[0].step.id) await showStepAt(hits[0].step.id, hits[0].anchor);
      else { renderWalk(); await openAnchor(hits[0].anchor); }
    } else {
      state.stepId = null;
      state.walkFocus = null;
      renderWalk();
      if (state.view) renderCode();
      renderFiles();
    }
    renderThread();
  }

  function moveStep(delta) {
    const next = L.moveStep(state.walk, state.stepId, delta);
    if (next) selectStep(next, { open: true });
  }

  async function openAnchor(anchor) {
    state.walkFocus = anchor;
    if (state.tab !== "walk") {
      state.tab = "walk";
      renderTabs();
    }
    await openFile(anchor.path);
    if (state.view && state.view.path === anchor.path) {
      renderCode();
      flashRows(L.anchorRows(state.view.rows, anchor));
    }
  }

  // --- sidebar ---------------------------------------------------------------

  function renderSidebar() {
    const box = $("findings");
    box.replaceChildren();
    const findings = state.detail.findings.findings;
    const grouped = L.groupFindings(findings, state.detail.decisions, state.showHidden);
    $("hidden-label").textContent = "Show refuted and rejected (" + grouped.hidden + ")";
    if (!findings.length) {
      box.appendChild(el("p", { class: "empty", text: "No findings: this is the plain diff. Pick a file, select lines, and ask about them." }));
    }
    for (const sev of L.SEVERITIES) {
      const items = grouped.groups[sev] || [];
      if (!items.length) continue;
      box.appendChild(el("h2", { class: "sev-h " + sev, text: sev + " (" + items.length + ")" }));
      for (const f of items) {
        const decision = state.detail.decisions[f.id];
        box.appendChild(el("button", {
          class: "finding-item" + (f.id === state.selected ? " selected" : "") +
            (L.isHidden(f, state.detail.decisions) ? " hidden-finding" : ""),
          "data-id": f.id,
          onclick: () => selectFinding(f.id),
        }, [
          el("span", { class: "badge " + sev, text: f.status }),
          decision ? el("span", { class: "badge " + decision.decision, text: decision.decision }) : null,
          el("span", { class: "claim", text: f.claim }),
          el("span", { class: "where", text: f.file + ":" + f.line_start }),
        ]));
      }
    }
    renderFiles();
  }

  function renderFiles() {
    const list = $("files");
    list.replaceChildren();
    const filter = state.fileFilter.toLowerCase();
    const shown = state.detail.files.filter((p) => !filter || p.toLowerCase().includes(filter));
    const inStep = state.tab === "walk" ? L.stepFiles(currentStep()) : new Set();
    for (const group of L.groupFilesByDir(shown)) {
      list.appendChild(el("li", { class: "dir-h", title: group.dir || "(repository root)", text: L.shortDir(group.dir) }));
      for (const f of group.files) {
        list.appendChild(el("li", {}, [el("button", {
          class: (f.path === state.filePath ? "selected" : "") + (inStep.has(f.path) ? " in-step" : ""),
          title: f.path,
          text: f.name,
          onclick: async () => { clearPick(); await openFile(f.path); await showFileReview(f.path); },
        })]));
      }
    }
    const current = list.querySelector("button.selected");
    if (current) current.scrollIntoView({ block: "nearest" });
  }

  // --- sidebar width ---------------------------------------------------------

  const SIDE_KEY = "review-viewer.side-width";
  const SIDE_DEFAULT = 280;

  function sideLimits() {
    return [160, Math.max(200, Math.round(window.innerWidth * 0.5))];
  }

  /* Sets the width through the CSSOM: the CSP forbids style attributes, not this. */
  function setSideWidth(px, save) {
    const [min, max] = sideLimits();
    const w = L.clampWidth(px, min, max);
    if (w == null) return;
    document.documentElement.style.setProperty("--side-w", w + "px");
    $("side-resize").setAttribute("aria-valuenow", String(w));
    if (save) {
      try { localStorage.setItem(SIDE_KEY, String(w)); } catch (err) { /* private window */ }
    }
  }

  function sideWidth() {
    return $("side").getBoundingClientRect().width;
  }

  function bindResize() {
    const handle = $("side-resize");
    let saved = null;
    try { saved = localStorage.getItem(SIDE_KEY); } catch (err) { /* private window */ }
    if (saved) setSideWidth(saved, false);
    handle.addEventListener("pointerdown", (ev) => {
      ev.preventDefault();
      handle.setPointerCapture(ev.pointerId);
      const startX = ev.clientX;
      const startW = sideWidth();
      document.body.classList.add("resizing");
      const move = (e) => setSideWidth(startW + e.clientX - startX, false);
      const up = () => {
        handle.removeEventListener("pointermove", move);
        handle.removeEventListener("pointerup", up);
        handle.removeEventListener("pointercancel", up);
        document.body.classList.remove("resizing");
        setSideWidth(sideWidth(), true);
      };
      handle.addEventListener("pointermove", move);
      handle.addEventListener("pointerup", up);
      handle.addEventListener("pointercancel", up);
    });
    handle.addEventListener("keydown", (ev) => {
      const step = ev.shiftKey ? 64 : 16;
      if (ev.key === "ArrowLeft") setSideWidth(sideWidth() - step, true);
      else if (ev.key === "ArrowRight") setSideWidth(sideWidth() + step, true);
      else return;
      ev.preventDefault();
    });
    handle.addEventListener("dblclick", () => setSideWidth(SIDE_DEFAULT, true));
  }

  // --- finding detail ----------------------------------------------------------

  async function selectFinding(id, opts) {
    state.selected = id;
    if (state.tab !== "finding" && !(opts && opts.stay)) {
      state.tab = "finding";
      renderTabs();
    }
    clearPick();
    renderSidebar();
    renderDetail();
    const f = state.findingsById.get(id);
    if (f) await openFile(f.file, f.line_start, f.line_end);
  }

  function citationLink(c) {
    return el("button", {
      class: "cite-link",
      text: L.anchorLabel(c) + (c.quote ? "  " + c.quote.split("\n")[0] : ""),
      onclick: () => openFile(c.path, c.line_start, c.line_end),
    });
  }

  function renderDetail() {
    const box = $("finding");
    box.replaceChildren();
    const f = state.selected && state.findingsById.get(state.selected);
    $("decide").hidden = !f || state.tab !== "finding";
    if (!f) {
      box.appendChild(el("p", { class: "muted", text: state.detail.findings.findings.length
        ? "Pick a finding on the left, or select lines in the code to ask about them."
        : "Select lines in the code (drag, or click a line number and shift-click another) to ask about them." }));
      renderThread();
      return;
    }
    box.appendChild(el("div", {}, [
      el("span", { class: "badge " + f.severity, text: f.severity }),
      el("span", { class: "badge", text: f.status }),
    ]));
    box.appendChild(el("h2", { text: f.claim }));
    box.appendChild(el("div", { class: "label", text: "Failure scenario" }));
    box.appendChild(el("p", { text: f.failure_scenario }));
    if (f.verdict_reason) {
      box.appendChild(el("div", { class: "label", text: "Verdict" }));
      box.appendChild(el("p", { text: f.verdict_reason }));
    }
    box.appendChild(el("div", { class: "label", text: "Cited code" }));
    for (const c of f.citations) box.appendChild(citationLink(c));
    const steps = L.stepsForFinding(state.walk, f.id);
    if (steps.length) {
      box.appendChild(el("div", { class: "label", text: "Explained in the walkthrough" }));
      for (const s of steps) {
        box.appendChild(el("button", { class: "cite-link", text: s.title, onclick: () => selectStep(s.id, { open: true }) }));
      }
    }
    if (f.fix) {
      box.appendChild(el("div", { class: "label", text: "Fix" }));
      box.appendChild(el("p", { text: f.fix.description }));
      if (f.fix.patch_sketch) box.appendChild(el("pre", { class: "sketch", text: f.fix.patch_sketch }));
      if (f.fix.premises.length) {
        box.appendChild(el("div", { class: "label", text: "Premises" }));
        const ul = el("ul");
        for (const p of f.fix.premises) {
          ul.appendChild(el("li", {}, [
            p.kind === "external" ? el("span", { class: "assumption", text: "Assumption: " }) : null,
            p.statement,
            p.citation ? citationLink(p.citation) : null,
          ]));
        }
        box.appendChild(ul);
      }
      if (f.fix.open_questions.length) {
        box.appendChild(el("div", { class: "label", text: "Open questions" }));
        const ul = el("ul");
        for (const q of f.fix.open_questions) ul.appendChild(el("li", { text: q }));
        box.appendChild(ul);
      }
    }
    renderDecision();
    renderThread();
  }

  function renderDecision() {
    const f = state.selected && state.findingsById.get(state.selected);
    if (!f) return;
    const d = state.detail.decisions[f.id];
    $("decision-now").textContent = d ? "— " + d.decision + (d.note ? ": " + d.note : "") : "— none yet";
  }

  async function decide(decision) {
    const id = state.selected;
    if (!id) return;
    const note = $("decision-note").value.trim();
    try {
      const res = await api("/api/decision", { target: state.slug, finding_id: id, decision: decision, note: note });
      state.detail.decisions[id] = res.decision;
      $("decision-note").value = "";
      renderSidebar();
      renderDecision();
    } catch (err) {
      showBanner("Could not record the decision: " + err.message);
    }
  }

  // --- threads ---------------------------------------------------------------

  function askingAboutStep() {
    return state.tab === "walk" && !!currentStep();
  }

  /* What a chat message was about, for its label. */
  function chatContext(q) {
    if (q.anchor) return L.walkAnchorLabel(q.anchor);
    if (q.step_id) {
      const st = state.walk && state.walk.steps.find((x) => x.id === q.step_id);
      return "Review: " + (st ? st.title : q.step_id);
    }
    if (q.finding_id) {
      const f = state.findingsById.get(q.finding_id);
      return "Finding: " + (f ? f.claim : q.finding_id);
    }
    return "The whole change";
  }

  /* The chat in the footer, and the one-line status beside the message box. */
  function renderThread() {
    renderAskAbout();
    const agent = state.who ? state.who.agent : "the session";
    const open = state.chatOpen;
    if (open) for (const q of state.questions) if (state.replies.has(q.id) && !L.isPending(q.id, state.replies)) state.seen.add(q.id);
    const st = L.chatStatus(state.questions, state.replies, state.seen);
    const status = $("chat-status");
    status.replaceChildren();
    if (st.pending) status.append(el("span", { class: "spinner" }), agent + " is answering" + (st.pending > 1 ? " " + st.pending + " messages" : "") + "…");
    else if (st.unread) status.append(el("span", { class: "unread-dot" }), st.unread + " new answer" + (st.unread > 1 ? "s" : "") + " — click the box to read");
    $("chat-title").textContent = "Chat with " + agent;
    if (!open) return;
    const list = $("thread");
    const chat = $("chat");
    const atBottom = chat.scrollTop + chat.clientHeight >= chat.scrollHeight - 4;
    list.replaceChildren();
    const msgs = L.chatQuestions(state.questions);
    if (!msgs.length) {
      list.appendChild(el("li", { class: "muted" , text: "No messages yet. Ask about the whole change, or type @ to point at a file and lines." }));
    }
    for (const q of msgs) {
      const reply = state.replies.get(q.id);
      const answer = el("div", { class: "a" });
      if (reply && reply.text) renderMarkdown(answer, reply.text);
      if (L.isPending(q.id, state.replies)) {
        answer.appendChild(el("div", { class: "muted" }, [el("span", { class: "spinner" }), agent + " is answering…"]));
      }
      list.appendChild(el("li", {}, [
        el("div", { class: "q" }, [el("span", { class: "anchor", text: chatContext(q) }), q.text]),
        answer,
      ]));
    }
    if (atBottom || state.chatJustOpened) chat.scrollTop = chat.scrollHeight;
    state.chatJustOpened = false;
  }

  function setChatOpen(open) {
    if (state.chatOpen === open) return;
    state.chatOpen = open;
    state.chatJustOpened = open;
    $("chat").hidden = !open;
    $("composer").classList.toggle("open", open);
    $("question").rows = open ? 3 : 1;
    renderThread();
  }

  function bindChat() {
    const footer = $("composer");
    // Focus from a click into the box opens the chat. Focus that insertRef
    // gives the box (a line clicked in the code) does not, so the chat does
    // not cover the code while more lines are being picked; typing opens it.
    footer.addEventListener("focusin", () => {
      if (state.quietFocus) { state.quietFocus = false; return; }
      setChatOpen(true);
    });
    $("question").addEventListener("mousedown", () => setChatOpen(true));
    $("question").addEventListener("input", () => setChatOpen(true));
    footer.addEventListener("focusout", () => {
      // Focus moving within the footer (the chat, its links) keeps it open.
      setTimeout(() => { if (!footer.contains(document.activeElement)) setChatOpen(false); }, 0);
    });
    $("chat-close").addEventListener("click", () => { document.activeElement.blur(); setChatOpen(false); });
    footer.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape" && !state.ac) {
        ev.stopPropagation();
        document.activeElement.blur();
        setChatOpen(false);
      }
    });
  }

  async function send() {
    const box = $("question");
    const text = box.value.trim();
    if (!text) return;
    const anchor = L.refAnchor(L.parseRefs(text, state.detail.files));
    const stepId = anchor ? null : (askingAboutStep() ? state.stepId : null);
    const body = { target: state.slug, text: text, finding_id: anchor || stepId || state.tab !== "finding" ? null : state.selected,
      anchor: anchor, step_id: stepId };
    $("send").disabled = true;
    try {
      const res = await api("/api/ask", body);
      state.questions.push({ id: res.id, ts: new Date().toISOString(), kind: "question", text: text,
        finding_id: body.finding_id, anchor: anchor, step_id: stepId });
      state.chatJustOpened = true;
      box.value = "";
      closeAc();
      renderThread();
      schedulePoll(0);
    } catch (err) {
      showBanner("Could not send the question: " + err.message);
    } finally {
      $("send").disabled = false;
    }
  }

  /* At most one poll runs at a time. A poll asked for while one is in
   * flight runs once after it; a response that arrives after the target
   * changed is dropped (its generation no longer matches). */
  function pollOnce() {
    if (state.polling) {
      state.pollAgain = true;
      return state.polling;
    }
    state.polling = (async () => {
      try {
        do {
          state.pollAgain = false;
          await pollRequest();
        } while (state.pollAgain);
      } finally {
        state.polling = null;
      }
    })();
    return state.polling;
  }

  async function pollRequest() {
    const gen = state.pollGen;
    const asked = state.since;
    const res = await api("/api/poll?target=" + encodeURIComponent(state.slug) + "&since=" + asked);
    if (gen !== state.pollGen) return;
    const next = L.applyPoll({ rows: state.replyRows, since: state.since }, asked, res);
    state.replyRows = next.rows;
    state.since = next.since;
    if (next.reset) {
      state.pollAgain = true;
      return;
    }
    if (next.changed) {
      state.replies = L.groupReplies(state.replyRows);
      renderThread();
      if (state.view && L.explainByBlock(state.questions, state.view.path).size) redrawCode();
    }
  }

  function schedulePoll(delay) {
    clearTimeout(state.pollTimer);
    // An unfinished walkthrough counts as pending: its steps arrive while the
    // session writes them.
    const pending = state.questions.filter((q) => L.isPending(q.id, state.replies)).length +
      (state.walk && state.walk.complete ? 0 : 1);
    const wait = delay != null ? delay : L.pollDelay(pending);
    state.pollTimer = setTimeout(async () => {
      try { await Promise.all([pollOnce(), pollWalk()]); } catch (err) { /* banner already shown */ }
      schedulePoll();
    }, wait);
  }

  // --- code pane ---------------------------------------------------------------

  async function openFile(path, start, end) {
    let view = state.views.get(path);
    if (!view) {
      try {
        view = await api(targetUrl("/file") + "?path=" + encodeURIComponent(path));
      } catch (err) {
        $("file-path").textContent = path;
        $("file-flags").textContent = "";
        $("code").replaceChildren(el("p", { class: "notice", text: "Cannot show this file: " + err.message }));
        return;
      }
      state.views.set(path, view);
    }
    if (state.filePath !== path) {
      state.filePath = path;
      state.view = view;
      renderCode();
      renderFiles();
    }
    if (start != null) scrollToLines(start, end == null ? start : end);
  }

  function findingsIn(path) {
    return state.detail.findings.findings.filter((f) => f.file === path &&
      (state.showHidden || !L.isHidden(f, state.detail.decisions)));
  }

  function renderCode() {
    const view = state.view;
    $("file-path").textContent = view.path;
    const flags = [];
    if (view.deleted) flags.push("deleted at head; first lines shown");
    if (view.truncated && !view.deleted) flags.push("large file: changes and cited lines only");
    if (view.binary) flags.push("binary");
    $("file-flags").textContent = flags.join(" · ");
    const code = $("code");
    if (view.binary) {
      code.replaceChildren(el("p", { class: "notice", text: "Binary file: no text to show." }));
      return;
    }
    const textRows = view.rows.filter((r) => r.k === "ctx" || r.k === "add");
    const delRows = view.rows.filter((r) => r.k === "del");
    const textHtml = highlight(textRows.map((r) => r.t).join("\n"), view.language);
    const delHtml = highlight(delRows.map((r) => r.t).join("\n"), view.language);
    // Highlighting runs over the whole file so folds do not break a token's
    // context; each row keeps its own html whether it is shown or folded.
    let ti = 0;
    let di = 0;
    const rowHtml = view.rows.map((r) => (r.k === "gap" ? L.escapeHtml(r.t)
      : r.k === "del" ? delHtml[di++] || "" : textHtml[ti++] || ""));
    const cited = view.cited_ranges || [];
    const selected = state.selected && state.findingsById.get(state.selected);
    const dots = new Map();
    for (const f of findingsIn(view.path)) {
      if (!dots.has(f.line_start)) dots.set(f.line_start, []);
      dots.get(f.line_start).push(f);
    }
    const wf = state.tab === "walk" && state.walkFocus && state.walkFocus.path === view.path ? state.walkFocus : null;
    const walkRows = new Set(wf ? L.anchorRows(view.rows, wf) : []);
    // Rows that must never be folded away: cited lines, the selected finding,
    // finding dots, the lines picked for a question, the walkthrough anchor.
    const keep = new Set(walkRows);
    for (const [a, b] of cited) for (const i of L.citationRows(view.rows, a, b)) keep.add(i);
    if (selected && selected.file === view.path) {
      for (const i of L.citationRows(view.rows, selected.line_start, selected.line_end)) keep.add(i);
    }
    for (const n of dots.keys()) for (const i of L.citationRows(view.rows, n, n)) keep.add(i);
    if (state.pick && state.pick.path === view.path) {
      for (const i of L.citationRows(view.rows, state.pick.start, state.pick.end)) keep.add(i);
    }
    const open = openRows(view.path);
    const items = L.foldRows(view.rows, keep, open, FOLD_CONTEXT, FOLD_MIN);
    const starts = L.blockStarts(view.rows);
    const explained = L.explainByBlock(state.questions, view.path);
    state.shownRows = new Set(items.filter((it) => it.row != null).map((it) => it.row));
    const tbody = el("tbody");
    for (const it of items) {
      if (it.fold) {
        tbody.appendChild(foldRow(view, it.fold));
        continue;
      }
      const ri = it.row;
      const r = view.rows[ri];
      if (starts.has(ri)) tbody.appendChild(blockHead(view.path, starts.get(ri), explained));
      const classes = [r.k];
      if (r.n != null && cited.some(([a, b]) => r.n >= a && r.n <= b)) classes.push("cited");
      if (selected && selected.file === view.path && r.n != null &&
          r.n >= selected.line_start && r.n <= selected.line_end) classes.push("focus");
      if (state.pick && state.pick.path === view.path && r.n != null &&
          r.n >= state.pick.start && r.n <= state.pick.end) classes.push("picked");
      if (walkRows.has(ri)) classes.push("walk-focus");
      const dotCell = el("td", { class: "mark" });
      for (const f of (r.n != null && dots.get(r.n)) || []) {
        dotCell.appendChild(el("span", {
          class: "dot " + f.severity, title: f.severity + ": " + f.claim,
          onclick: () => selectFinding(f.id),
        }));
      }
      const src = el("td", { class: "src" });
      src.innerHTML = rowHtml[ri];
      tbody.appendChild(el("tr", { class: classes.join(" "), "data-ri": String(ri),
        "data-n": r.n == null ? null : String(r.n), "data-o": r.o == null ? null : String(r.o) }, [
        dotCell,
        el("td", { class: "ln", text: r.o == null ? "" : String(r.o) }),
        el("td", { class: "ln new", text: r.n == null ? "" : String(r.n) }),
        el("td", { class: "mark" }),
        src,
      ]));
    }
    code.replaceChildren(fileNav(-1), el("table", {}, [tbody]), fileNav(1));
  }

  // --- explaining one block ------------------------------------------------------

  const EXPLAIN_TEXT = "Explain this block: what it changes and why.";

  /* The row above a block of changed lines: a light-bulb button, or, once
   * asked, the session's explanation (a spinner until it arrives). */
  function blockHead(path, range, explained) {
    const q = explained.get(L.blockKey(range));
    const td = el("td", { colspan: "5" });
    if (!q) {
      td.appendChild(el("button", {
        class: "bulb", title: "Ask " + (state.who ? state.who.agent : "the session") + " to explain " + L.formatRef(path, range),
        "aria-label": "Explain this block", text: "💡 Explain",
        onclick: () => explainBlock(path, range),
      }));
    } else {
      const reply = state.replies.get(q.id);
      const body = el("div", { class: "strip-body" });
      if (reply && reply.text) renderMarkdown(body, reply.text);
      if (L.isPending(q.id, state.replies)) {
        body.appendChild(el("div", { class: "muted" }, [el("span", { class: "spinner" }),
          (state.who ? state.who.agent : "The session") + " is explaining this block…"]));
      }
      td.appendChild(el("div", { class: "strip" }, [
        el("span", { class: "strip-icon", text: "💡" }),
        body,
        el("button", { class: "cite-link strip-more", title: "Put this block into the question box",
          text: "Follow up", onclick: () => insertRef(path, range) }),
      ]));
    }
    return el("tr", { class: "block-head" }, [td]);
  }

  async function explainBlock(path, range) {
    const anchor = { path: path, side: range.side, line_start: range.line_start, line_end: range.line_end };
    try {
      const res = await api("/api/ask", { target: state.slug, text: EXPLAIN_TEXT, anchor: anchor, explain: true });
      state.questions.push({ id: res.id, kind: "question", text: EXPLAIN_TEXT, anchor: anchor, explain: true });
      redrawCode();
      schedulePoll(0);
    } catch (err) {
      showBanner("Could not ask for an explanation: " + err.message);
    }
  }

  /* Re-render the code pane without moving it. */
  function redrawCode() {
    if (!state.view) return;
    const top = $("code").scrollTop;
    renderCode();
    $("code").scrollTop = top;
  }

  // --- folds -------------------------------------------------------------------

  const FOLD_CONTEXT = 3;
  const FOLD_MIN = 4;
  const FOLD_STEP = 20;

  function openRows(path) {
    if (!state.openRows.has(path)) state.openRows.set(path, new Set());
    return state.openRows.get(path);
  }

  function foldLabel(view, fold) {
    const [from, to] = fold;
    const a = view.rows[from];
    const b = view.rows[to];
    const count = to - from + 1;
    const lines = a.n != null && b.n != null ? "lines " + a.n + "–" + b.n : count + " lines";
    return "⋯ " + lines + " unchanged (" + count + ")";
  }

  function foldRow(view, fold) {
    const count = fold[1] - fold[0] + 1;
    const expand = (how) => {
      const open = openRows(view.path);
      for (const i of L.expandFold(fold, how, FOLD_STEP)) open.add(i);
      const keepTop = $("code").scrollTop;
      renderCode();
      $("code").scrollTop = keepTop;
    };
    const buttons = [];
    if (count > FOLD_STEP) {
      buttons.push(el("button", { class: "fold-btn", title: "Show " + FOLD_STEP + " more lines above", text: "↑ " + FOLD_STEP, onclick: () => expand("up") }));
      buttons.push(el("button", { class: "fold-btn", title: "Show " + FOLD_STEP + " more lines below", text: "↓ " + FOLD_STEP, onclick: () => expand("down") }));
    }
    buttons.push(el("button", { class: "fold-btn", text: "Show all", onclick: () => expand("all") }));
    return el("tr", { class: "fold" }, [
      el("td", { colspan: "5" }, [el("span", { class: "fold-label", text: foldLabel(view, fold) }), ...buttons]),
    ]);
  }

  // --- moving between files --------------------------------------------------

  function neighbour(delta) {
    const order = L.fileOrder(state.detail.files, state.fileFilter);
    return L.neighbourFile(order, state.filePath, delta);
  }

  /* The bar above (delta -1) or below (delta 1) a file, naming the file
   * that scrolling on past this edge opens. */
  function fileNav(delta) {
    const path = neighbour(delta);
    const box = el("div", { class: "file-nav " + (delta > 0 ? "next" : "prev") });
    if (!path) {
      if (delta > 0) box.appendChild(el("span", { class: "muted", text: "End of the last changed file." }));
      return box;
    }
    const name = path.slice(path.lastIndexOf("/") + 1);
    box.appendChild(el("button", {
      class: "file-nav-btn", title: path,
      text: delta > 0 ? "Keep scrolling for the next file: " + name + " ▼" : "▲ Previous file: " + name,
      onclick: () => goFile(delta),
    }));
    box.appendChild(el("div", { class: "overscroll-bar" }));
    return box;
  }

  async function goFile(delta) {
    const path = neighbour(delta);
    if (!path) return;
    clearPickState();
    await openFile(path);
    if (state.tab === "walk") await showFileReview(path);
    const code = $("code");
    code.scrollTop = delta > 0 ? 0 : code.scrollHeight;
  }

  function showOverscroll(dir, progress) {
    for (const bar of $("code").querySelectorAll(".overscroll-bar")) bar.style.width = "0";
    if (!dir) return;
    const bar = $("code").querySelector(".file-nav." + (dir > 0 ? "next" : "prev") + " .overscroll-bar");
    if (bar) bar.style.width = Math.round(Math.min(1, progress) * 100) + "%";
  }

  function onCodeWheel(ev) {
    if (!state.view || ev.ctrlKey) return;
    const code = $("code");
    const atTop = code.scrollTop <= 0;
    const atBottom = code.scrollTop + code.clientHeight >= code.scrollHeight - 1;
    const edge = ev.deltaY > 0 && atBottom ? 1 : ev.deltaY < 0 && atTop ? -1 : 0;
    const res = L.overscroll(state.overscroll, edge, ev.deltaY, performance.now());
    state.overscroll = res.acc;
    showOverscroll(res.move ? 0 : res.acc.dir, res.progress || 0);
    if (res.move) goFile(ev.deltaY > 0 ? 1 : -1);
  }

  function scrollToLines(start, end) {
    if (!state.view) return;
    flashRows(L.citationRows(state.view.rows, start, end));
  }

  function flashRows(idx) {
    if (!idx.length || !state.view) return;
    if (idx.some((i) => !state.shownRows.has(i))) {
      // The lines to show are inside a fold: open them, with some context.
      const open = openRows(state.view.path);
      for (const i of idx) for (let j = i - FOLD_CONTEXT; j <= i + FOLD_CONTEXT; j++) open.add(j);
      renderCode();
    }
    const code = $("code");
    const first = code.querySelector('tr[data-ri="' + idx[0] + '"]');
    if (first) first.scrollIntoView({ block: "center" });
    for (const i of idx) {
      const tr = code.querySelector('tr[data-ri="' + i + '"]');
      if (!tr) continue;
      tr.classList.remove("flash");
      void tr.offsetWidth;  // restart the animation
      tr.classList.add("flash");
    }
  }

  // --- asking about selected lines ------------------------------------------------

  function setPick(a, b, x, y) {
    const [start, end] = L.lineRange(a, b);
    state.pick = { path: state.view.path, start: start, end: end };
    renderCode();
    const btn = $("ask-lines");
    const centre = btn.parentElement.getBoundingClientRect();
    btn.style.left = Math.max(8, Math.min(x - centre.left, centre.width - 200)) + "px";
    btn.style.top = Math.max(8, y - centre.top + 12) + "px";
    btn.hidden = false;
  }

  function clearPickState() {
    state.pick = null;
    $("ask-lines").hidden = true;
    const sel = window.getSelection();
    if (sel) sel.removeAllRanges();
  }

  function clearPick() {
    clearPickState();
    if (state.view) renderCode();
    if (state.detail) renderThread();
  }

  function rowNumber(node) {
    const tr = node && (node.nodeType === 1 ? node : node.parentElement);
    const row = tr && tr.closest("tr[data-n]");
    return row ? Number(row.getAttribute("data-n")) : null;
  }

  function onCodeMouseUp(ev) {
    if (ev.target.closest("td.ln, .dot")) return;
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed) return;
    const a = rowNumber(sel.anchorNode);
    const b = rowNumber(sel.focusNode);
    if (a == null || b == null) return;
    setPick(a, b, ev.clientX, ev.clientY);
  }

  function onCodeClick(ev) {
    const cell = ev.target.closest("td.ln");
    if (!cell) {
      // A click (not a drag) on an added or deleted row puts its whole
      // contiguous block into the question.
      const tr = ev.target.closest("tr.add[data-ri], tr.del[data-ri]");
      const sel = window.getSelection();
      if (!tr || ev.target.closest(".dot, button") || (sel && !sel.isCollapsed)) return;
      const block = L.diffBlock(state.view.rows, Number(tr.getAttribute("data-ri")));
      if (block) insertRef(state.view.path, block);
      return;
    }
    const n = rowNumber(cell);
    if (n == null) return;
    const start = ev.shiftKey && state.pick && state.pick.path === state.view.path ? state.pick.start : n;
    setPick(start, n, ev.clientX, ev.clientY);
  }

  function askAboutPick() {
    if (!state.pick) return;
    insertRef(state.pick.path, { side: "head", line_start: state.pick.start, line_end: state.pick.end });
    $("ask-lines").hidden = true;
  }

  // --- the question box (footer) ---------------------------------------------

  /* Say so when a message will be filed under the open review or finding
   * (the chat labels it that way); say nothing otherwise, including when
   * the text holds @references, which speak for themselves. */
  function renderAskAbout() {
    const box = $("ask-about");
    if (!box || !state.detail) return;
    const anchor = L.refAnchor(L.parseRefs($("question").value, state.detail.files));
    const f = state.selected && state.findingsById.get(state.selected);
    const step = askingAboutStep() ? currentStep() : null;
    box.textContent = anchor ? ""
      : step ? "Linked to review: " + step.title
        : state.tab === "finding" && f ? "Linked to finding: " + f.claim : "";
  }

  /* Put "@path:lines" into the question at the caret, with spaces around it. */
  function insertRef(path, ref) {
    const box = $("question");
    const at = document.activeElement === box ? box.selectionStart : null;
    const merged = L.mergeRef(box.value, state.detail.files, path, ref, at);
    box.value = merged.text;
    const caret = merged.caret;
    if (document.activeElement !== box) state.quietFocus = true;
    box.focus();
    state.quietFocus = false;  // focusin already ran, or never will (window not focused)
    box.setSelectionRange(caret, caret);
    closeAc();
    renderAskAbout();
  }

  function closeAc() {
    state.ac = null;
    $("ac").hidden = true;
    $("question").removeAttribute("aria-activedescendant");
  }

  function updateAc() {
    const box = $("question");
    const c = L.completion(box.value, box.selectionStart);
    const items = c ? L.matchFiles(state.detail.files, c.query, 8) : [];
    if (!c || !items.length) { closeAc(); return; }
    const keep = state.ac && state.ac.query === c.query ? Math.min(state.ac.index, items.length - 1) : 0;
    state.ac = { start: c.start, query: c.query, items: items, index: keep };
    renderAc();
  }

  function renderAc() {
    const list = $("ac");
    list.replaceChildren();
    state.ac.items.forEach((path, i) => {
      const cut = path.lastIndexOf("/");
      list.appendChild(el("li", {
        id: "ac-" + i, role: "option", class: i === state.ac.index ? "active" : "",
        "aria-selected": String(i === state.ac.index), title: path,
        onmousedown: (ev) => { ev.preventDefault(); pickAc(i); },
      }, [el("span", { class: "ac-name", text: path.slice(cut + 1) }),
        cut > 0 ? el("span", { class: "ac-dir", text: path.slice(0, cut) }) : null]));
    });
    list.hidden = false;
    $("question").setAttribute("aria-activedescendant", "ac-" + state.ac.index);
    const active = list.querySelector("li.active");
    if (active) active.scrollIntoView({ block: "nearest" });
  }

  /* Replace the "@query" being typed with "@path"; the caret stays right
   * after it, so ":12" can follow. */
  function pickAc(i) {
    const box = $("question");
    const ac = state.ac;
    if (!ac) return;
    const path = ac.items[i];
    const caret = box.selectionStart;
    const head = box.value.slice(0, ac.start) + "@" + path;
    box.value = head + box.value.slice(caret);
    box.focus();
    box.setSelectionRange(head.length, head.length);
    closeAc();
    renderAskAbout();
  }

  function onQuestionKey(ev) {
    if (state.ac) {
      const n = state.ac.items.length;
      if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
        ev.preventDefault();
        state.ac.index = (state.ac.index + (ev.key === "ArrowDown" ? 1 : n - 1)) % n;
        renderAc();
        return;
      }
      if ((ev.key === "Enter" && !ev.ctrlKey && !ev.metaKey) || ev.key === "Tab") {
        ev.preventDefault();
        pickAc(state.ac.index);
        return;
      }
      if (ev.key === "Escape") {
        ev.preventDefault();
        ev.stopPropagation();
        closeAc();
        return;
      }
    }
    if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); send(); }
  }

  // --- events ------------------------------------------------------------------

  function bindEvents() {
    bindResize();
    bindChat();
    $("send").addEventListener("click", send);
    $("question").addEventListener("keydown", onQuestionKey);
    $("question").addEventListener("input", () => { updateAc(); renderAskAbout(); });
    $("question").addEventListener("click", updateAc);
    $("question").addEventListener("blur", () => setTimeout(closeAc, 100));
    for (const b of document.querySelectorAll("[data-decision]")) {
      b.addEventListener("click", () => decide(b.getAttribute("data-decision")));
    }
    $("show-hidden").addEventListener("change", (ev) => {
      state.showHidden = ev.target.checked;
      renderSidebar();
      if (state.view) renderCode();
    });
    $("file-search").addEventListener("input", (ev) => {
      state.fileFilter = ev.target.value;
      renderFiles();
    });
    $("code").addEventListener("mouseup", onCodeMouseUp);
    $("code").addEventListener("wheel", onCodeWheel, { passive: true });
    $("code").addEventListener("click", onCodeClick);
    $("ask-lines").addEventListener("click", askAboutPick);
    $("tab-finding").addEventListener("click", () => setTab("finding"));
    $("tab-summary").addEventListener("click", () => setTab("summary"));
    $("tab-walk").addEventListener("click", () => setTab("walk"));
    document.addEventListener("keydown", (ev) => {
      const typing = ev.target.closest && ev.target.closest("input, textarea, select");
      if (ev.key === "Escape") {
        if (typing) ev.target.blur();
        clearPick();
        return;
      }
      if (typing || ev.metaKey || ev.ctrlKey || ev.altKey) return;
      if (ev.key === "j" || ev.key === "k") {
        const order = L.findingOrder(L.groupFindings(state.detail.findings.findings,
          state.detail.decisions, state.showHidden));
        const next = L.stepFinding(order, state.selected, ev.key === "j" ? 1 : -1);
        if (state.tab !== "finding") setTab("finding");
        if (next && next !== state.selected) selectFinding(next);
      } else if (ev.key === "]" || ev.key === "[") {
        state.tabChosen = true;
        moveStep(ev.key === "]" ? 1 : -1);
      } else if (ev.key === "/") {
        ev.preventDefault();
        $("file-search").focus();
      }
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    boot().catch((err) => showBanner("The viewer could not load: " + err.message));
  });
})();
