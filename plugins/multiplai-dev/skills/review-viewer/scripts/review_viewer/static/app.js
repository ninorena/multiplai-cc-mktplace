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
  const REQUEST_TIMEOUT_MS = 8000;
  const FATAL = {
    auth: { icon: "🔒", title: "This page is not authorised",
      text: "The page needs the one-time link the viewer printed when it started.",
      fix: "Open the \"open: file://…\" link from the session again." },
    gone: { icon: "", title: "Reconnecting to the viewer…",
      text: "The viewer server is not answering. This page reconnects by itself when it is back.",
      fix: "It stops after 30 minutes without an open page; if it does not come back, ask the session to start it again." },
  };

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
    chatJustOpened: false,
    seen: new Set(),
    badgeOpen: null,
    tabChosen: false,
    walk: null,
    walkKey: "",
    stepId: null,
    walkFocus: null,
    viewed: {},
    split: false,
    wrap: false,
    blockRows: [],
    blockPin: null,
    shownMsgs: new Set(),
    fatal: null,
    toastTimer: null,
    palette: { items: [], index: 0 },
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

  /* A short message that goes away by itself: a request that failed. */
  function showToast(text) {
    const t = $("toast");
    t.textContent = text;
    t.hidden = !text;
    clearTimeout(state.toastTimer);
    if (text) state.toastTimer = setTimeout(() => { t.hidden = true; }, 6000);
  }

  /* A card over the whole page for the two states nothing on it works in:
   * no token ("auth"), or the server not answering ("gone"). */
  function showFatal(kind) {
    state.fatal = kind || null;
    const box = $("fatal");
    box.hidden = !kind;
    if (!kind) return;
    const f = FATAL[kind];
    $("fatal-icon").replaceChildren(f.icon ? f.icon : el("span", { class: "spinner" }));
    $("fatal-title").textContent = f.title;
    $("fatal-text").textContent = f.text;
    $("fatal-fix").textContent = f.fix;
  }

  async function api(path, body) {
    const opts = { headers: { "X-Review-Token": state.token }, cache: "no-store" };
    if (body !== undefined) {
      opts.method = "POST";
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    // A stopped server behind the container's address can leave a request
    // hanging instead of refusing it, so every request gives up after a while.
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), REQUEST_TIMEOUT_MS);
    opts.signal = ctrl.signal;
    let res;
    try {
      res = await fetch(path, opts);
    } catch (err) {
      showFatal("gone");
      throw err;
    } finally {
      clearTimeout(timer);
    }
    if (res.status === 401) {
      showFatal("auth");
      throw new Error("unauthorised");
    }
    if (state.fatal === "gone") showFatal(null);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "HTTP " + res.status);
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

  /* Copy without the Clipboard API, which a page served over plain http
   * (the container's .orb.local address) does not get. */
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    const area = el("textarea", { class: "copy-scratch", "aria-hidden": "true" });
    area.value = text;
    document.body.appendChild(area);
    area.select();
    try { document.execCommand("copy"); } finally { area.remove(); }
    return Promise.resolve();
  }

  function addCopyButtons(node) {
    for (const pre of node.querySelectorAll("pre")) {
      const btn = el("button", { class: "copy-btn", type: "button", text: "Copy", title: "Copy this block" });
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        const code = pre.querySelector("code") || pre;
        copyText(code.textContent).then(() => {
          btn.textContent = "Copied";
          setTimeout(() => { btn.textContent = "Copy"; }, 1500);
        }).catch(() => showToast("Could not copy."));
      });
      pre.appendChild(btn);
    }
  }

  function renderMarkdown(node, text) {
    if (window.marked && window.DOMPurify) {
      node.innerHTML = window.DOMPurify.sanitize(window.marked.parse(text));
      if (window.hljs) node.querySelectorAll("pre code").forEach((c) => window.hljs.highlightElement(c));
      addCopyButtons(node);
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
    // The mode theme.js resolved (the header's button, or the system setting).
    const mode = document.documentElement.getAttribute("data-mode");
    const dark = mode ? mode === "dark"
      : window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
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
      showFatal("auth");
      return;
    }
    try {
      state.who = await api("/api/whoami");
    } catch (err) {
      return;
    }
    const res = await api("/api/targets");
    state.targets = res.targets;
    const sel = $("target-select");
    if (state.targets.length > 1) {
      sel.hidden = false;
      for (const t of state.targets) sel.appendChild(el("option", { value: t.slug, text: t.label }));
      sel.addEventListener("change", () => loadTarget(sel.value));
    }
    state.split = prefs.load(SPLIT_KEY) === "1";
    state.wrap = prefs.load(WRAP_KEY) === "1";
    bindEvents();
    // Every 10 s; every 3 s while the server is away, so the page comes back by itself.
    let tick = 0;
    setInterval(() => {
      tick += 1;
      if (state.fatal === "gone" || tick % 3 === 0) api("/api/alive").catch(() => {});
    }, 3000);
    if (state.targets.length) await loadTarget(state.targets[0].slug);
    schedulePoll();
  }

  async function loadTarget(slug) {
    state.pollGen += 1;
    state.slug = slug;
    state.detail = await api(targetUrl());
    state.findingsById = new Map(state.detail.findings.findings.map((f) => [f.id, f]));
    state.viewed = state.detail.viewed || {};
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
    state.blockPin = null;
    if (!state.tabChosen) state.tab = "summary";
    const target = state.detail.findings.target;
    $("title").textContent = target.label;
    document.title = target.label + " · review";
    renderHeader();
    renderFindingList();
    renderFiles();
    renderTabs();
    renderWalk();
    renderSummary();
    schedulePrRefresh();
    state.walkSince = Date.now();
    const walkGen = state.pollGen;
    setTimeout(() => {
      if (walkGen !== state.pollGen || state.walk) return;
      renderWalk();
      renderSummary();
    }, WALK_WAIT_MS + 50);
    await Promise.all([pollOnce(), pollWalk()]);
    const first = L.findingOrder(L.groupFindings(state.detail.findings.findings, state.detail.decisions, state.showHidden))[0];
    if (first) await selectFinding(first, { stay: true });
    else {
      renderDetail();
      if (state.detail.files.length) await openFile(state.detail.files[0]);
    }
  }

  // --- PR checks: asked again every minute while any is still running ---------------

  const PR_REFRESH_MS = 60000;

  function schedulePrRefresh() {
    clearTimeout(state.prTimer);
    const pr = state.detail.pr;
    if (!pr || !pr.checks || !pr.checks.pending) return;
    const gen = state.pollGen;
    state.prTimer = setTimeout(async () => {
      if (gen !== state.pollGen) return;
      try {
        const res = await api(targetUrl("/pr"));
        if (gen !== state.pollGen) return;
        state.detail.pr = res.pr;
        state.detail.stats = res.stats;
        renderHeader();
        renderSummary();
      } catch (err) {
        // A server that is gone shows its own card; try again next minute.
      }
      schedulePrRefresh();
    }, PR_REFRESH_MS);
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
    if (state.view) renderFileNav();
    for (const [name, [tab, panel]] of Object.entries(TABS)) {
      const on = state.tab === name;
      $(tab).setAttribute("aria-selected", String(on));
      $(tab).classList.toggle("active", on);
      $(panel).hidden = !on;
    }
    const n = state.walk ? state.walk.steps.length : 0;
    $("walk-count").textContent = state.walk ? String(n) : "";
    $("walk-count").classList.toggle("live", !state.walk || !state.walk.complete);
    $("walk-count").title = !state.walk ? "Waiting for the session" : state.walk.complete ? "" : "Still being written";
    const nf = state.detail ? state.detail.findings.findings.length : 0;
    $("finding-count").textContent = nf ? String(nf) : "";
  }

  // --- waiting states ----------------------------------------------------------------

  /* Grey bars where text will be, with a line saying what is being waited for. */
  function skeleton(caption, widths) {
    const box = el("div", { class: "skeleton", "aria-busy": "true" },
      [el("div", { class: "skel-caption" }, [el("span", { class: "spinner" }), caption])]);
    for (const w of widths || ["w90", "w75", "w90", "w60"]) box.appendChild(el("span", { class: "skel " + w }));
    return box;
  }

  /* The page cannot see whether the session is writing a walkthrough. The
   * session starts one within a minute or two of serving the page, so after
   * WALK_WAIT_MS with nothing written, stop claiming it is on the way. */
  const WALK_WAIT_MS = 3 * 60 * 1000;
  const NO_WALK_TEXT = "Nothing has been written yet, so the session may not be writing a walkthrough. " +
    "Ask for one in the chat below.";

  function walkOverdue() {
    return !state.walk && Date.now() - state.walkSince >= WALK_WAIT_MS;
  }

  function emptyState(icon, text) {
    return el("div", { class: "empty-state" }, [el("span", { class: "big", text: icon }), text]);
  }

  // --- risk ------------------------------------------------------------------------

  const RISK_BADGE = { high: "concern", medium: "note", low: "good" };

  /* The risk score, or null until the walkthrough has its risk block. */
  function currentRisk() {
    const r = L.riskInputs(state.detail.stats, state.walk, state.detail.findings.findings,
      state.detail.decisions, state.detail.findings.target.files_changed);
    return r ? Object.assign({ inputs: r }, L.riskLevel(r)) : null;
  }

  function riskWord(level) {
    return level.charAt(0).toUpperCase() + level.slice(1);
  }

  /* Markdown for the risk badge's detail: the rules that fired, then every input. */
  function riskDetail(risk) {
    const r = risk.inputs;
    const findingsLine = r.openHigh + " HIGH, " + r.openMedium + " MEDIUM (confirmed and not rejected)";
    return "**" + riskWord(risk.level) + "** because:\n\n" + risk.reasons.map((x) => "- " + x).join("\n") +
      "\n\n**Inputs**\n\n" + [
        "Touches tier " + r.tier + ", " + L.TIER_NAMES[r.tier] + ": " + r.tierWhy,
        (r.revertable ? "A revert undoes it: " : "A revert cannot undo it: ") + r.revertWhy,
        "Tests: " + (r.tests || "not assessed yet"),
        "PR checks: " + (state.detail.pr ? (r.checksFailing ? "failing" : "not failing") : "no PR"),
        "Size: " + (r.large ? "large" : "not large"),
        "Open findings: " + findingsLine,
      ].map((x) => "- " + x).join("\n") +
      "\n\nThe rules are fixed; the review-viewer skill's SKILL.md lists them.";
  }

  function renderRisk() {
    const pill = $("risk-pill");
    const risk = currentRisk();
    pill.hidden = false;
    pill.className = "risk-pill " + (risk ? RISK_BADGE[risk.level] : "pending");
    pill.textContent = risk ? "Risk: " + riskWord(risk.level) : "Risk: not scored yet";
    pill.title = risk ? risk.reasons.join("; ") : "Scored once " + (state.who ? state.who.agent : "the session") +
      " writes the walkthrough's risk block.";
    pill.onclick = () => {
      state.badgeOpen = risk ? "risk" : null;
      setTab("summary");
      renderSummary();
    };
    return risk;
  }

  // --- summary ---------------------------------------------------------------------

  function renderSummary() {
    const agent = state.who ? state.who.agent : "the session";
    const risk = renderRisk();
    const badges = L.summaryBadges(state.detail.stats, state.walk);
    if (risk) {
      badges.unshift({ key: "risk", label: "Risk: " + riskWord(risk.level) + " · " + risk.reasons[0],
        level: RISK_BADGE[risk.level], detail: riskDetail(risk), source: "risk" });
    }
    const box = $("badges");
    box.replaceChildren();
    const groups = [["risk", "Risk of merging"],
      ["measured", "Measured from git" + (state.detail.pr ? " and GitHub" : "")],
      ["assessed", "Assessed by " + agent]];
    for (const [source, title] of groups) {
      const mine = badges.filter((b) => b.source === source);
      const row = el("div", { class: "badge-row" }, [el("div", { class: "label", text: title })]);
      if (!mine.length) {
        if (source === "risk") {
          row.appendChild(el("span", { class: "muted small", text: "Scored once " + agent +
            " writes the walkthrough's risk block." }));
        } else if (source === "assessed" && (!state.walk || !state.walk.complete) && !walkOverdue()) {
          for (let i = 0; i < 3; i++) row.appendChild(el("span", { class: "skel pill" }));
        } else {
          row.appendChild(el("span", { class: "muted small", text: source === "measured"
            ? "Not available: git could not read these commits." : "None." }));
        }
      }
      for (const b of mine) {
        row.appendChild(el("button", {
          class: "qbadge " + b.level + (state.badgeOpen === b.key ? " open" : ""),
          title: source === "measured" ? b.detail : source === "risk" ? "Click for the rules and inputs"
            : b.label + " (click for " + agent + "'s reasoning)",
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
      if (open.source !== "measured") renderMarkdown(detail, open.detail);
      else detail.replaceChildren(el("p", { text: open.detail }));
    }
    const overview = $("walk-overview");
    if (!state.walk) {
      overview.replaceChildren(el("div", { class: "label", text: "Overview" }), walkOverdue()
        ? emptyState("✎", NO_WALK_TEXT)
        : skeleton((state.who ? state.who.agent : "The session") + " is reading the diff and writing the overview"));
    } else {
      renderMarkdown(overview, state.walk.overview_md);
      overview.insertBefore(el("div", { class: "label", text: "Overview" }), overview.firstChild);
      const cov = L.walkCoverage(state.walk, state.detail.files, state.detail.findings.findings);
      overview.appendChild(el("p", { class: "coverage", text: "The reviews cover " + cov.files + " of " + cov.filesTotal +
        " changed files" + (cov.findingsTotal ? " and link " + cov.findings + " of " + cov.findingsTotal + " findings" : "") + "." }));
      if (!state.walk.complete) {
        overview.appendChild(el("div", { class: "skel-caption" }, [el("span", { class: "spinner" }),
          "Still writing: " + state.walk.steps.length + " review" + (state.walk.steps.length === 1 ? "" : "s") + " so far"]));
      }
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
    if (state.view) renderFileNav();
    const list = $("walk-steps");
    list.replaceChildren();
    if (!state.walk) {
      $("walk-step").replaceChildren(walkOverdue()
        ? emptyState("✎", NO_WALK_TEXT)
        : skeleton((state.who ? state.who.agent : "The session") + " is writing the reviews",
          ["w75", "w60", "w75", "w40"]));
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
    if (!state.walk.complete) {
      list.appendChild(el("li", { class: "skel-caption" }, [el("span", { class: "spinner" }), "Writing more reviews…"]));
    }
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
        box.appendChild(emptyState("☝", "Pick a review above, click a file, or press ] to start."));
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
        box.appendChild(el("div", { class: "step-finding" }, [
          el("button", {
            class: "finding-item finding-card " + f.severity, "data-id": f.id,
            title: "Open on the Findings tab to decide",
            onclick: () => { setTab("finding"); selectFinding(f.id); },
          }, [
            el("span", { class: "badge " + f.severity, text: f.severity }),
            el("span", { class: "badge", text: f.status }),
            el("span", { class: "claim", text: f.claim }),
            el("span", { class: "where", text: f.file + ":" + f.line_start }),
          ]),
          el("div", { class: "step-finding-facts" }, findingFacts(f)),
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

  // --- findings list (top of the Findings tab) -----------------------------------

  function renderFindingList() {
    const box = $("findings");
    box.replaceChildren();
    const findings = state.detail.findings.findings;
    const grouped = L.groupFindings(findings, state.detail.decisions, state.showHidden);
    $("hidden-label").textContent = "Show refuted and rejected (" + grouped.hidden + ")";
    $("show-hidden").parentElement.hidden = !grouped.hidden;
    if (!findings.length) {
      box.appendChild(emptyState("✓", "No code review for these commits: this is the plain diff. " +
        "Select lines in the code to ask about them."));
    }
    for (const sev of L.SEVERITIES) {
      const items = grouped.groups[sev] || [];
      if (!items.length) continue;
      box.appendChild(el("h2", { class: "sev-h " + sev, text: sev + " (" + items.length + ")" }));
      for (const f of items) {
        box.appendChild(el("button", {
          class: "finding-item " + sev + (f.id === state.selected ? " selected" : "") +
            (L.isHidden(f, state.detail.decisions) ? " hidden-finding" : ""),
          "data-id": f.id,
          onclick: async () => {
            await selectFinding(f.id);
            $("finding-detail").scrollIntoView({ block: "start", behavior: "smooth" });
          },
        }, [
          el("span", { class: "badge " + sev, text: f.status }),
          state.detail.decisions[f.id] ? el("span", {
            class: "badge " + state.detail.decisions[f.id].decision,
            text: state.detail.decisions[f.id].decision,
          }) : null,
          el("span", { class: "claim", text: f.claim }),
          el("span", { class: "where", text: f.file + ":" + f.line_start }),
        ]));
      }
    }
  }

  // --- file status, counts, viewed ----------------------------------------------

  const STATUS_WORD = { A: "added", M: "modified", D: "deleted", R: "renamed", C: "copied", T: "type changed" };

  function fileInfo(path) {
    const per = state.detail.stats && state.detail.stats.per_file;
    return (per && per[path]) || null;
  }

  function statusMark(info) {
    const st = (info && info.status) || "M";
    return el("span", { class: "fstatus " + st, title: STATUS_WORD[st] || st, text: st });
  }

  function countsNode(info) {
    const box = el("span", { class: "counts" });
    if (!info) return box;
    if (info.added == null) box.appendChild(el("span", { class: "muted", text: "bin" }));
    else {
      box.appendChild(el("span", { class: "plus", text: "+" + info.added }));
      box.appendChild(el("span", { class: "minus", text: "−" + (info.deleted || 0) }));
    }
    return box;
  }

  function isViewed(path) {
    return Object.prototype.hasOwnProperty.call(state.viewed, path);
  }

  async function setViewed(path, viewed) {
    const before = state.viewed;
    // Show it at once; the server's answer replaces it.
    state.viewed = Object.assign({}, before);
    if (viewed) state.viewed[path] = new Date().toISOString();
    else delete state.viewed[path];
    renderFiles();
    renderFileHead();
    try {
      const res = await api("/api/viewed", { target: state.slug, path: path, viewed: viewed });
      state.viewed = res.viewed;
    } catch (err) {
      state.viewed = before;
      showToast("Could not save \u201cviewed\u201d: " + err.message);
    }
    renderFiles();
    renderFileHead();
  }

  function renderFiles() {
    if (state.view) renderFileNav();
    const list = $("files");
    list.replaceChildren();
    const filter = state.fileFilter.toLowerCase();
    const shown = state.detail.files.filter((p) => !filter || p.toLowerCase().includes(filter));
    const inStep = state.tab === "walk" ? L.stepFiles(currentStep()) : new Set();
    for (const group of L.groupFilesByDir(shown)) {
      list.appendChild(el("li", { class: "dir-h", title: group.dir || "(repository root)", text: L.shortDir(group.dir) }));
      for (const f of group.files) {
        const info = fileInfo(f.path);
        const viewed = isViewed(f.path);
        const box = el("input", { type: "checkbox", title: viewed ? "Viewed; click to unmark" : "Mark viewed",
          "aria-label": "Viewed: " + f.path });
        box.checked = viewed;
        box.addEventListener("change", () => setViewed(f.path, box.checked));
        list.appendChild(el("li", {
          class: "file-row" + (f.path === state.filePath ? " selected" : "") + (inStep.has(f.path) ? " in-step" : "") +
            (viewed ? " viewed" : ""),
        }, [
          statusMark(info),
          el("button", {
            class: "file-btn", title: f.path + (info ? " (" + (STATUS_WORD[info.status] || info.status) + ")" : ""),
            text: f.name,
            onclick: async () => { clearPick(); await openFile(f.path); await showFileReview(f.path); },
          }),
          countsNode(info),
          box,
        ]));
      }
    }
    if (!shown.length) list.appendChild(el("li", { class: "empty small", text: "No changed file matches." }));
    const vc = L.viewedCount(state.detail.files, state.viewed);
    $("viewed-count").textContent = vc.done + " of " + vc.total + " viewed";
    $("viewed-bar").style.width = (vc.total ? Math.round(100 * vc.done / vc.total) : 0) + "%";
    const current = list.querySelector(".file-row.selected");
    if (current) current.scrollIntoView({ block: "nearest" });
  }

  // --- panel widths ----------------------------------------------------------

  /* The two side panels; the code in the middle takes what is left. `dir` is
   * which way the handle moves to widen the panel. */
  const PANELS = {
    side: { handle: "side-resize", cssVar: "--side-w", key: "review-viewer.side-width", def: 280, min: 160, dir: 1 },
    detail: { handle: "detail-resize", cssVar: "--detail-w", key: "review-viewer.detail-width", def: 380, min: 240, dir: -1 },
  };

  function panelLimits(p) {
    return [p.min, Math.max(p.min + 40, Math.round(window.innerWidth * 0.5))];
  }

  /* Widths and the code layout are saved where theme.js saves the theme, so
   * every viewer shares them. */
  const prefs = window.ReviewPrefs || { load: () => "", save: () => {} };
  const SPLIT_KEY = "review-viewer.split";
  const WRAP_KEY = "review-viewer.wrap";

  /* Sets the width through the CSSOM: the CSP forbids style attributes, not this. */
  function setPanelWidth(name, px, save) {
    const p = PANELS[name];
    const [min, max] = panelLimits(p);
    const w = L.clampWidth(px, min, max);
    if (w == null) return;
    document.documentElement.style.setProperty(p.cssVar, w + "px");
    $(p.handle).setAttribute("aria-valuenow", String(w));
    if (save) prefs.save(p.key, String(w));
  }

  function panelWidth(name) {
    return $(name).getBoundingClientRect().width;
  }

  /* Hiding a side panel leaves a strip along its edge that brings it back.
   * Saved with the widths. */
  function setPanelHidden(name, hidden, save) {
    const panel = $(name);
    if (hidden && panel.contains(document.activeElement)) document.activeElement.blur();
    $("layout").classList.toggle(name + "-hidden", hidden);
    if (save) prefs.save("review-viewer." + name + "-hidden", hidden ? "1" : "");
    if (!hidden) $(name === "side" ? "side-hide" : "detail-hide").focus({ preventScroll: true });
  }

  function togglePanel(name) {
    setPanelHidden(name, !$("layout").classList.contains(name + "-hidden"), true);
  }

  function bindPanels() {
    for (const name of Object.keys(PANELS)) {
      if (prefs.load("review-viewer." + name + "-hidden") === "1") setPanelHidden(name, true, false);
      $(name + "-hide").addEventListener("click", () => setPanelHidden(name, true, true));
      $(name + "-rail").addEventListener("click", () => setPanelHidden(name, false, true));
    }
  }

  function bindResize() {
    bindPanels();
    for (const [name, p] of Object.entries(PANELS)) {
      const handle = $(p.handle);
      const saved = prefs.load(p.key);
      if (saved) setPanelWidth(name, saved, false);
      handle.addEventListener("pointerdown", (ev) => {
        ev.preventDefault();
        handle.setPointerCapture(ev.pointerId);
        const startX = ev.clientX;
        const startW = panelWidth(name);
        document.body.classList.add("resizing");
        const move = (e) => setPanelWidth(name, startW + p.dir * (e.clientX - startX), false);
        const up = () => {
          handle.removeEventListener("pointermove", move);
          handle.removeEventListener("pointerup", up);
          handle.removeEventListener("pointercancel", up);
          document.body.classList.remove("resizing");
          setPanelWidth(name, panelWidth(name), true);
        };
        handle.addEventListener("pointermove", move);
        handle.addEventListener("pointerup", up);
        handle.addEventListener("pointercancel", up);
      });
      handle.addEventListener("keydown", (ev) => {
        const step = ev.shiftKey ? 64 : 16;
        if (ev.key === "ArrowLeft") setPanelWidth(name, panelWidth(name) - p.dir * step, true);
        else if (ev.key === "ArrowRight") setPanelWidth(name, panelWidth(name) + p.dir * step, true);
        else return;
        ev.preventDefault();
      });
      handle.addEventListener("dblclick", () => setPanelWidth(name, p.def, true));
    }
  }

  // --- finding detail ----------------------------------------------------------

  async function selectFinding(id, opts) {
    state.selected = id;
    if (state.tab !== "finding" && !(opts && opts.stay)) {
      state.tab = "finding";
      renderTabs();
    }
    clearPick();
    renderFindingList();
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

  /* What a finding says beyond its claim: the failure scenario, the expected
   * behaviour, the verdict and the cited code. Shown on the Findings tab and
   * under each walkthrough step that links the finding. */
  function findingFacts(f) {
    const out = [el("div", { class: "label", text: "Failure scenario" }), el("p", { text: f.failure_scenario })];
    if (f.expected_behaviour) {
      out.push(el("div", { class: "label", text: "Expected behaviour" }), el("p", { text: f.expected_behaviour }));
    }
    if (f.verdict_reason) out.push(el("div", { class: "label", text: "Verdict" }), el("p", { text: f.verdict_reason }));
    out.push(el("div", { class: "label", text: "Cited code" }));
    for (const c of f.citations) out.push(citationLink(c));
    return out;
  }

  function renderDetail() {
    const box = $("finding-detail");
    box.replaceChildren();
    const f = state.selected && state.findingsById.get(state.selected);
    if (!f) {
      if (state.detail.findings.findings.length) box.appendChild(emptyState("☝", "Pick a finding above, or press j."));
      renderThread();
      return;
    }
    box.appendChild(el("div", {}, [
      el("span", { class: "badge " + f.severity, text: f.severity }),
      el("span", { class: "badge", text: f.status }),
    ]));
    box.appendChild(el("h2", { text: f.claim }));
    for (const node of findingFacts(f)) box.appendChild(node);
    const steps = L.stepsForFinding(state.walk, f.id);
    if (steps.length) {
      box.appendChild(el("div", { class: "label", text: "Explained in the walkthrough" }));
      for (const s of steps) {
        box.appendChild(el("button", { class: "cite-link", text: s.title, onclick: () => selectStep(s.id, { open: true }) }));
      }
    }
    box.appendChild(decisionBox(f));
    renderThread();
  }

  // --- accept / reject / defer -------------------------------------------------

  function decisionBox(f) {
    const d = state.detail.decisions[f.id];
    const note = el("input", { type: "text", class: "decision-note", placeholder: "Note (optional)",
      "aria-label": "Decision note" });
    const buttons = ["accept", "reject", "defer"].map((k) => el("button", {
      class: "ctl",
      "aria-pressed": String(!!d && d.decision === k),
      text: k[0].toUpperCase() + k.slice(1),
      onclick: () => decide(f.id, k, note.value.trim()),
    }));
    return el("section", { class: "decide" }, [
      el("div", { class: "label", text: "Decision" }),
      el("p", { class: "muted", text: d ? d.decision + (d.note ? ": " + d.note : "") : "None yet." }),
      note,
      el("div", { class: "decide-row" }, buttons),
    ]);
  }

  async function decide(id, decision, note) {
    try {
      const res = await api("/api/decision", { target: state.slug, finding_id: id, decision: decision, note: note });
      state.detail.decisions[id] = res.decision;
      renderFindingList();
      renderDetail();
      // renderSummary rebuilds the Summary tab's risk badge and calls renderRisk
      // for the header pill, so both show the score after this decision.
      renderSummary();
    } catch (err) {
      showToast("Could not record the decision: " + err.message);
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
    else if (st.unread) status.append(el("span", { class: "unread-dot" }), st.unread + " new answer" + (st.unread > 1 ? "s" : "") + " · press c to read");
    $("chat-title").textContent = "Chat with " + agent;
    renderFab();
    if (!open) return;
    const list = $("thread");
    const atBottom = list.scrollTop + list.clientHeight >= list.scrollHeight - 4;
    list.replaceChildren();
    const msgs = L.chatQuestions(state.questions);
    if (!msgs.length) {
      list.appendChild(el("li", {}, [emptyState("💬", "No messages yet. Ask about the whole change, " +
        "or type @ to point at a file and lines.")]));
    }
    for (const q of msgs) {
      const reply = state.replies.get(q.id);
      const pending = L.isPending(q.id, state.replies);
      const answer = el("div", { class: "a" });
      if (reply && reply.text) renderMarkdown(answer, reply.text);
      if (pending) {
        answer.appendChild(el("div", { class: "muted" }, [el("span", { class: "spinner" }), agent + " is answering…"]));
      }
      // Slide in only what was not on screen before, and an answer when it lands.
      const key = q.id + (pending ? ":p" : ":d");
      const isNew = !state.shownMsgs.has(key);
      state.shownMsgs.add(key);
      list.appendChild(el("li", { class: isNew ? "new" : "" }, [
        el("div", { class: "q-wrap" }, [
          el("div", { class: "msg-meta" }, [
            el("span", { class: "anchor", text: chatContext(q), title: chatContext(q) }),
            el("span", { class: "who", text: "You" }),
            q.ts ? el("time", { datetime: q.ts, text: clock(q.ts) }) : null,
          ]),
          el("div", { class: "q", text: q.text }),
        ]),
        el("div", { class: "a-wrap" }, [
          el("div", { class: "msg-meta" }, [
            el("span", { class: "who", text: agent }),
            reply && reply.ts ? el("time", { datetime: reply.ts, text: clock(reply.ts) }) : null,
          ]),
          answer,
        ]),
      ]));
    }
    if (atBottom || state.chatJustOpened) list.scrollTop = list.scrollHeight;
    state.chatJustOpened = false;
  }

  /* "14:02" today, "3 Oct 14:02" on another day, in the viewer's time zone. */
  function clock(iso) {
    const d = new Date(iso);
    if (isNaN(d)) return "";
    const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    return d.toDateString() === new Date().toDateString() ? time
      : d.toLocaleDateString([], { day: "numeric", month: "short" }) + " " + time;
  }

  /* The drawer opens from the round button, `c`, or "Ask about these lines",
   * and stays open until it is closed: clicking the code does not close it. */
  function setChatOpen(open) {
    if (state.chatOpen === open) return;
    state.chatOpen = open;
    state.chatJustOpened = open;
    const drawer = $("composer");
    drawer.classList.toggle("open", open);
    drawer.setAttribute("aria-hidden", String(!open));
    $("chat-fab").setAttribute("aria-expanded", String(open));
    if (open) {
      const box = $("question");
      box.focus();
      box.setSelectionRange(box.value.length, box.value.length);
    } else if (drawer.contains(document.activeElement)) {
      document.activeElement.blur();
    }
    renderThread();
  }

  /* The round button: a spinner while an answer is being written, a dot for
   * unread answers, and a note beside it for those or for an unsent draft. */
  function renderFab() {
    const st = L.chatStatus(state.questions, state.replies, state.seen);
    const open = state.chatOpen;
    $("chat-fab").parentElement.hidden = open;
    $("chat-fab").classList.toggle("busy", !!st.pending);
    $("fab-dot").hidden = !st.unread;
    const draft = $("question").value.trim();
    const note = $("fab-note");
    const text = st.unread ? st.unread + " new answer" + (st.unread > 1 ? "s" : "") + " · press c"
      : draft ? "Unsent message · " + $("ask-about").textContent : "";
    note.textContent = text;
    note.hidden = open || !text;
  }

  function bindChat() {
    const footer = $("composer");
    $("chat-fab").addEventListener("click", () => setChatOpen(true));
    $("chat-close").addEventListener("click", () => setChatOpen(false));
    footer.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape" && !state.ac) {
        ev.stopPropagation();
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
      showToast("Could not send the question: " + err.message);
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

  /* Above the code: status, path, line counts, viewed, and the layout buttons. */
  function renderFileHead() {
    const view = state.view;
    if (!view) return;
    const info = fileInfo(view.path);
    const st = $("file-status");
    st.hidden = !info;
    if (info) {
      st.className = "fstatus " + info.status;
      st.textContent = info.status;
      st.title = STATUS_WORD[info.status] || info.status;
    }
    $("file-path").textContent = view.path;
    $("file-path").title = view.path;
    $("file-counts").replaceChildren(...countsNode(info).childNodes);
    const flags = [];
    if (view.deleted) flags.push("deleted at head; first lines shown");
    if (view.truncated && !view.deleted) flags.push("large file: changes and cited lines only");
    if (view.binary) flags.push("binary");
    $("file-flags").textContent = flags.join(" · ");
    $("file-viewed").checked = isViewed(view.path);
    $("layout-unified").setAttribute("aria-pressed", String(!state.split));
    $("layout-split").setAttribute("aria-pressed", String(state.split));
    $("wrap-btn").setAttribute("aria-pressed", String(state.wrap));
    renderFileNav();
  }

  function renderCode() {
    const view = state.view;
    renderFileHead();
    const code = $("code");
    code.classList.toggle("split", state.split);
    code.classList.toggle("wrap", state.wrap);
    if (view.binary) {
      state.blockRows = [];
      state.rowEls = [];
      code.replaceChildren(emptyState("▦", "Binary file: no text to show."));
      updateBlockLine();
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
    // A deleted line and the added line that replaces it: mark the words that differ.
    for (const [ri, partner] of L.changePairs(view.rows)) {
      if (view.rows[ri].k !== "del") continue;
      const d = L.wordDiff(view.rows[ri].t, view.rows[partner].t);
      if (!d) continue;
      rowHtml[ri] = L.markRanges(rowHtml[ri], d.del, "wd-del");
      rowHtml[partner] = L.markRanges(rowHtml[partner], d.add, "wd-add");
    }
    const cited = view.cited_ranges || [];
    const oldNum = L.oldNumbers(view.rows);
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
    state.blockRows = [...starts.keys()].filter((ri) => state.shownRows.has(ri)).sort((x, y) => x - y);

    /* Row classes that depend on the head line number (or the row itself). */
    const marks = (r, ri) => {
      const out = [];
      if (!r) return out;
      if (r.n != null && cited.some(([a, b]) => r.n >= a && r.n <= b)) out.push("cited");
      if (selected && selected.file === view.path && r.n != null &&
          r.n >= selected.line_start && r.n <= selected.line_end) out.push("focus");
      if (state.pick && state.pick.path === view.path && r.n != null &&
          r.n >= state.pick.start && r.n <= state.pick.end) out.push("picked");
      if (walkRows.has(ri)) out.push("walk-focus");
      return out;
    };
    const dotCell = (r) => {
      const td = el("td", { class: "dots" });
      for (const f of (r && r.n != null && dots.get(r.n)) || []) {
        td.appendChild(el("span", {
          class: "dot " + f.severity, title: f.severity + ": " + f.claim,
          onclick: () => selectFinding(f.id),
        }));
      }
      return td;
    };
    const cols = state.split ? 7 : 5;
    const tbody = el("tbody");
    if (!state.split) {
      for (const it of items) {
        if (it.fold) {
          tbody.appendChild(foldRow(view, it.fold, cols));
          continue;
        }
        const ri = it.row;
        const r = view.rows[ri];
        if (starts.has(ri)) tbody.appendChild(blockHead(view.path, starts.get(ri), explained, cols));
        const src = el("td", { class: "src" });
        src.innerHTML = rowHtml[ri];
        tbody.appendChild(el("tr", { class: [r.k].concat(marks(r, ri)).join(" "), "data-ri": String(ri),
          "data-n": r.n == null ? null : String(r.n), "data-o": r.o == null ? null : String(r.o) }, [
          dotCell(r),
          el("td", { class: "ln", text: oldNum[ri] == null ? "" : String(oldNum[ri]) }),
          el("td", { class: "ln new", text: r.n == null ? "" : String(r.n) }),
          el("td", { class: "mark" }),
          src,
        ]));
      }
    } else {
      const side = (r) => (!r ? " none" : r.k === "del" ? " del" : r.k === "add" ? " add" : "");
      const srcCell = (r, ri, which) => {
        const td = el("td", { class: "src " + which + side(r),
          "data-ri": r && (r.k === "add" || r.k === "del") ? String(ri) : null });
        if (r) td.innerHTML = rowHtml[ri];
        return td;
      };
      for (const line of L.splitLines(view.rows, items)) {
        if (line.fold) {
          tbody.appendChild(foldRow(view, line.fold, cols));
          continue;
        }
        if (line.gap != null) {
          tbody.appendChild(el("tr", { class: "gap", "data-ri": String(line.gap) },
            [el("td", { colspan: String(cols), text: view.rows[line.gap].t })]));
          continue;
        }
        const li = line.left;
        const ri = line.right;
        const lr = li != null ? view.rows[li] : null;
        const rr = ri != null ? view.rows[ri] : null;
        const first = Math.min(li == null ? Infinity : li, ri == null ? Infinity : ri);
        if (starts.has(first)) tbody.appendChild(blockHead(view.path, starts.get(first), explained, cols));
        const kind = li === ri ? "ctx" : "chg";
        tbody.appendChild(el("tr", { class: [kind].concat(marks(rr, ri), rr ? [] : marks(lr, li)).join(" "),
          "data-ri": String(ri != null ? ri : li), "data-ril": li != null && li !== ri ? String(li) : null,
          "data-n": rr && rr.n != null ? String(rr.n) : null, "data-o": lr && lr.o != null ? String(lr.o) : null }, [
          dotCell(rr),
          el("td", { class: "ln" + side(lr), text: lr && oldNum[li] != null ? String(oldNum[li]) : "" }),
          el("td", { class: "mark" + side(lr) }),
          srcCell(lr, li, "left"),
          el("td", { class: "ln new" + side(rr), text: rr && rr.n != null ? String(rr.n) : "" }),
          el("td", { class: "mark" + side(rr) }),
          srcCell(rr, ri, "right"),
        ]));
      }
    }
    code.replaceChildren(el("table", {}, [tbody]));
    state.rowEls = [...code.querySelectorAll("tr[data-ri]")];
    updateBlockLine();
  }

  // --- where am I in the file: the enclosing scope and the change count ---------

  /* The row index under a point `offset` px below the top of the code pane. */
  function rowAt(offset) {
    const rows = state.rowEls || [];
    if (!rows.length) return null;
    const y = $("code").getBoundingClientRect().top + offset;
    let lo = 0;
    let hi = rows.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (rows[mid].getBoundingClientRect().bottom <= y) lo = mid + 1;
      else hi = mid;
    }
    return Number(rows[lo].getAttribute("data-ri"));
  }

  /* The reference line sits a little below the top, where a change that was
   * jumped to lands (see moveBlock). */
  const REF_OFFSET = 72;

  /* Which ends of its scroll range the code pane is against. A pane that
   * does not scroll is against both. */
  function paneEdge() {
    const code = $("code");
    return { top: code.scrollTop <= 1, bottom: code.scrollTop + code.clientHeight >= code.scrollHeight - 1 };
  }

  /* The block of changes Prev/Next last moved to in this file, by its first
   * row. It stays current while the pane cannot scroll that block up to the
   * reference line: near the end or the start of the file, or when the whole
   * file fits on screen. */
  function blockPin() {
    const pin = state.blockPin;
    return pin && state.view && pin.path === state.view.path ? pin.row : null;
  }

  function updateBlockLine() {
    const view = state.view;
    const starts = state.blockRows || [];
    const ri = view && !view.binary ? rowAt(REF_OFFSET) : null;
    const edge = paneEdge();
    const k = L.currentBlock(starts, ri, blockPin(), edge) + 1;
    const scope = ri != null ? L.enclosingScope(view.rows, ri) : null;
    $("block-where").textContent = scope ? "in " + scope : "";
    $("block-where").title = scope || "";
    $("block-pos").textContent = !starts.length ? "no changes shown"
      : k ? "change " + k + " of " + starts.length : starts.length + " change" + (starts.length === 1 ? "" : "s");
    $("block-prev").disabled = ri == null || L.stepCurrent(starts, ri, blockPin(), edge, -1) < 0;
    $("block-next").disabled = ri == null || L.stepCurrent(starts, ri, blockPin(), edge, 1) < 0;
  }

  function moveBlock(delta) {
    const ri = rowAt(REF_OFFSET);
    if (ri == null) return;
    const idx = L.stepCurrent(state.blockRows, ri, blockPin(), paneEdge(), delta);
    if (idx < 0) return;
    const target = state.blockRows[idx];
    const tr = rowEl(target);
    if (!tr) return;
    state.blockPin = { path: state.view.path, row: target };
    const code = $("code");
    const top = tr.getBoundingClientRect().top - code.getBoundingClientRect().top + code.scrollTop;
    code.scrollTo({ top: Math.max(0, top - REF_OFFSET + 8), behavior: reducedMotion() ? "auto" : "smooth" });
    tr.classList.remove("flash");
    void tr.offsetWidth;
    tr.classList.add("flash");
    // When the pane is already as far as it goes, no scroll event follows.
    updateBlockLine();
  }

  function reducedMotion() {
    return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }

  /* The table row showing row index `i` (in split view, either side). */
  function rowEl(i) {
    return $("code").querySelector('tr[data-ri="' + i + '"], tr[data-ril="' + i + '"]');
  }

  // --- explaining one block ------------------------------------------------------

  const EXPLAIN_TEXT = "Explain this block: what it changes and why.";

  /* The row above a block of changed lines: a light-bulb button, or, once
   * asked, the session's explanation (a spinner until it arrives). */
  function blockHead(path, range, explained, cols) {
    const q = explained.get(L.blockKey(range));
    const td = el("td", { colspan: String(cols) });
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
          text: "Follow up", onclick: () => insertRef(path, range, { open: true }) }),
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
      showToast("Could not ask for an explanation: " + err.message);
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

  function foldRow(view, fold, cols) {
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
      el("td", { colspan: String(cols) }, [el("span", { class: "fold-label", text: foldLabel(view, fold) }), ...buttons]),
    ]);
  }

  // --- moving between files --------------------------------------------------

  function navContext() {
    return L.navFiles(state.tab === "walk" ? currentStep() : null, state.detail.files,
      state.fileFilter, state.filePath);
  }

  /* Prev / Next beside the file name. */
  function renderFileNav() {
    const nav = navContext();
    const i = nav.order.indexOf(state.filePath);
    const where = nav.inStep ? " file in this review" : " file";
    for (const [id, d] of [["file-prev", -1], ["file-next", 1]]) {
      const path = i < 0 ? null : nav.order[i + d];
      const btn = $(id);
      btn.disabled = !path;
      btn.title = path ? (d < 0 ? "Previous" : "Next") + where + ": " + path
        : "No " + (d < 0 ? "previous" : "next") + where;
    }
    $("file-pos").textContent = i < 0 ? "" : (i + 1) + " of " + nav.order.length + (nav.inStep ? " in this review" : "");
  }

  async function goFile(delta) {
    const nav = navContext();
    const path = L.neighbourFile(nav.order, state.filePath, delta);
    if (!path) return;
    clearPickState();
    if (nav.inStep) {
      // Stay on the same review step: open the file at its first anchor there.
      await openAnchor(currentStep().anchors.find((a) => a.path === path));
      return;
    }
    await openFile(path);
    if (state.tab === "walk") await showFileReview(path);
    $("code").scrollTop = 0;
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
    const first = rowEl(idx[0]);
    if (first) first.scrollIntoView({ block: "center" });
    for (const i of idx) {
      const tr = rowEl(i);
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
      const hit = ev.target.closest("tr.add[data-ri], tr.del[data-ri], td.src.add[data-ri], td.src.del[data-ri]");
      const sel = window.getSelection();
      if (!hit || ev.target.closest(".dot, button") || (sel && !sel.isCollapsed)) return;
      const block = L.diffBlock(state.view.rows, Number(hit.getAttribute("data-ri")));
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
    insertRef(state.pick.path, { side: "head", line_start: state.pick.start, line_end: state.pick.end }, { open: true });
    $("ask-lines").hidden = true;
  }

  // --- the question box (footer) ---------------------------------------------

  /* What the next message is filed under, the same way send() decides:
   * @lines typed in it, else the open review step, else the open finding,
   * else the whole change. */
  function renderAskAbout() {
    const box = $("ask-about");
    if (!box || !state.detail) return;
    const anchor = L.refAnchor(L.parseRefs($("question").value, state.detail.files));
    const f = state.selected && state.findingsById.get(state.selected);
    const step = askingAboutStep() ? currentStep() : null;
    const what = anchor ? L.walkAnchorLabel(anchor)
      : step ? step.title
        : state.tab === "finding" && f ? f.claim : "the whole change";
    box.replaceChildren(el("span", { class: "muted", text: "Context: " }), what);
    box.title = "Context: " + what + ". Type @ to point at a file and lines instead.";
  }

  /* Put "@path:lines" into the question at the caret, with spaces around it. */
  /* Adds a line reference to the message. With the chat closed it stays
   * closed (more lines can be picked; the button shows the unsent message)
   * unless `open` is asked for. */
  function insertRef(path, ref, opts) {
    const box = $("question");
    const at = document.activeElement === box ? box.selectionStart : null;
    const merged = L.mergeRef(box.value, state.detail.files, path, ref, at);
    box.value = merged.text;
    if (opts && opts.open) setChatOpen(true);
    if (state.chatOpen) {
      box.focus();
      box.setSelectionRange(merged.caret, merged.caret);
    }
    closeAc();
    renderAskAbout();
    renderFab();
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

  // --- Go to (Ctrl+K) -------------------------------------------------------------

  function paletteEntries() {
    const out = [];
    for (const path of state.detail.files) {
      const cut = path.lastIndexOf("/");
      out.push({ kind: "file", label: path.slice(cut + 1), sub: cut > 0 ? path.slice(0, cut) : "", path: path });
    }
    ((state.walk && state.walk.steps) || []).forEach((st, i) => {
      out.push({ kind: "review", label: st.title, sub: "review " + (i + 1), step: st.id });
    });
    for (const f of state.detail.findings.findings) {
      if (!state.showHidden && L.isHidden(f, state.detail.decisions)) continue;
      out.push({ kind: f.severity, label: f.claim, sub: f.file + ":" + f.line_start, finding: f.id });
    }
    return out;
  }

  function renderPalette() {
    const list = $("palette-list");
    list.replaceChildren();
    const pal = state.palette;
    pal.items = L.paletteMatch(paletteEntries(), $("palette-input").value, 50);
    pal.index = Math.min(pal.index, Math.max(0, pal.items.length - 1));
    if (!pal.items.length) list.appendChild(el("li", { class: "none", text: "Nothing matches." }));
    pal.items.forEach((it, i) => {
      list.appendChild(el("li", {
        id: "pal-" + i, role: "option", class: i === pal.index ? "active" : "", "aria-selected": String(i === pal.index),
        onmousedown: (ev) => { ev.preventDefault(); pickPalette(i); },
        onmousemove: () => { if (pal.index !== i) { pal.index = i; markPalette(); } },
      }, [
        el("span", { class: "kind " + it.kind, text: it.kind === "file" ? "file" : it.kind === "review" ? "review" : it.kind.toLowerCase() }),
        el("span", { class: "label", text: it.label }),
        it.sub ? el("span", { class: "sub", text: it.sub }) : null,
      ]));
    });
    markPalette();
  }

  function markPalette() {
    const list = $("palette-list");
    list.querySelectorAll("li[role=option]").forEach((li, i) => {
      const on = i === state.palette.index;
      li.classList.toggle("active", on);
      li.setAttribute("aria-selected", String(on));
      if (on) li.scrollIntoView({ block: "nearest" });
    });
    $("palette-input").setAttribute("aria-activedescendant", "pal-" + state.palette.index);
  }

  function openPalette() {
    if (!state.detail) return;
    const dlg = $("palette");
    if (dlg.open) return;
    if ($("help").open) $("help").close();
    $("palette-input").value = "";
    state.palette.index = 0;
    renderPalette();
    dlg.showModal();
    $("palette-input").focus();
  }

  async function pickPalette(i) {
    const it = state.palette.items[i];
    $("palette").close();
    if (!it) return;
    if (it.path) { clearPick(); await openFile(it.path); await showFileReview(it.path); }
    else if (it.step) { state.tabChosen = true; await selectStep(it.step, { open: true }); }
    else if (it.finding) { setTab("finding"); await selectFinding(it.finding); }
  }

  function onPaletteKey(ev) {
    const pal = state.palette;
    const n = pal.items.length;
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault();
      if (n) pal.index = (pal.index + (ev.key === "ArrowDown" ? 1 : n - 1)) % n;
      markPalette();
    } else if (ev.key === "Enter") {
      ev.preventDefault();
      pickPalette(pal.index);
    }
  }

  // --- shortcuts ---------------------------------------------------------------

  function setSplit(on) {
    state.split = on;
    prefs.save(SPLIT_KEY, on ? "1" : "0");
    if (state.view) redrawCode();
  }

  function setWrap(on) {
    state.wrap = on;
    prefs.save(WRAP_KEY, on ? "1" : "0");
    if (state.view) redrawCode();
  }

  function toggleHelp() {
    const dlg = $("help");
    if (dlg.open) dlg.close();
    else dlg.showModal();
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
    $("show-hidden").addEventListener("change", (ev) => {
      state.showHidden = ev.target.checked;
      renderFindingList();
      if (state.view) renderCode();
    });
    $("file-search").addEventListener("input", (ev) => {
      state.fileFilter = ev.target.value;
      renderFiles();
    });
    $("code").addEventListener("mouseup", onCodeMouseUp);
    $("file-prev").addEventListener("click", () => goFile(-1));
    const code = $("code");
    const setCodeWidth = () => code.style.setProperty("--code-w", code.clientWidth + "px");
    setCodeWidth();
    new ResizeObserver(setCodeWidth).observe(code);
    window.addEventListener("resize", setCodeWidth);
    $("file-next").addEventListener("click", () => goFile(1));
    $("code").addEventListener("click", onCodeClick);
    $("ask-lines").addEventListener("click", askAboutPick);
    $("tab-finding").addEventListener("click", () => setTab("finding"));
    $("tab-summary").addEventListener("click", () => setTab("summary"));
    $("tab-walk").addEventListener("click", () => setTab("walk"));
    $("help-btn").addEventListener("click", toggleHelp);
    $("palette-input").addEventListener("input", () => { state.palette.index = 0; renderPalette(); });
    $("palette-input").addEventListener("keydown", onPaletteKey);
    $("palette").addEventListener("click", (ev) => { if (ev.target === $("palette")) $("palette").close(); });
    $("file-viewed").addEventListener("change", (ev) => { if (state.view) setViewed(state.view.path, ev.target.checked); });
    $("layout-unified").addEventListener("click", () => setSplit(false));
    $("layout-split").addEventListener("click", () => setSplit(true));
    $("wrap-btn").addEventListener("click", () => setWrap(!state.wrap));
    $("block-prev").addEventListener("click", () => moveBlock(-1));
    $("block-next").addEventListener("click", () => moveBlock(1));
    let scrollQueued = false;
    $("code").addEventListener("scroll", () => {
      if (scrollQueued) return;
      scrollQueued = true;
      requestAnimationFrame(() => { scrollQueued = false; updateBlockLine(); });
    }, { passive: true });
    $("help-close").addEventListener("click", () => $("help").close());
    // A click on the backdrop lands on the dialog itself, outside its content.
    $("help").addEventListener("click", (ev) => { if (ev.target === $("help")) $("help").close(); });
    document.addEventListener("keydown", (ev) => {
      if ((ev.ctrlKey || ev.metaKey) && !ev.shiftKey && ev.code === "KeyB") {
        ev.preventDefault();
        togglePanel(ev.altKey ? "detail" : "side");
        return;
      }
      if ((ev.ctrlKey || ev.metaKey) && !ev.altKey && !ev.shiftKey && (ev.key === "k" || ev.key === "K")) {
        ev.preventDefault();
        if ($("palette").open) $("palette").close();
        else openPalette();
        return;
      }
      if ($("palette").open) return;
      if ($("help").open) {
        if (ev.key === "?") { ev.preventDefault(); toggleHelp(); }
        return;  // Esc closes the dialog natively
      }
      const typing = ev.target.closest && ev.target.closest("input, textarea, select");
      if (ev.key === "Escape") {
        if (typing) ev.target.blur();
        if (state.chatOpen && !state.pick) setChatOpen(false);
        clearPick();
        return;
      }
      if (typing || ev.metaKey || ev.ctrlKey || ev.altKey) return;
      if (ev.key === "j" || ev.key === "k") {
        const order = L.findingOrder(L.groupFindings(state.detail.findings.findings, state.detail.decisions, state.showHidden));
        const next = L.stepFinding(order, state.selected, ev.key === "j" ? 1 : -1);
        if (state.tab !== "finding") setTab("finding");
        if (next && next !== state.selected) selectFinding(next);
      } else if (ev.key === "]" || ev.key === "[") {
        state.tabChosen = true;
        moveStep(ev.key === "]" ? 1 : -1);
      } else if (ev.key === "/") {
        ev.preventDefault();
        $("file-search").focus();
      } else if (ev.key === "?") {
        ev.preventDefault();
        toggleHelp();
      } else if (ev.key === "n" || ev.key === "p") {
        moveBlock(ev.key === "n" ? 1 : -1);
      } else if (ev.key === "v") {
        if (state.view) setViewed(state.view.path, !isViewed(state.view.path));
      } else if (ev.key === "s") {
        setSplit(!state.split);
      } else if (ev.key === "w") {
        setWrap(!state.wrap);
      } else if (ev.key === "c") {
        ev.preventDefault();
        setChatOpen(!state.chatOpen);
      }
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    boot().catch((err) => showToast("The viewer could not load: " + err.message));
  });
})();
