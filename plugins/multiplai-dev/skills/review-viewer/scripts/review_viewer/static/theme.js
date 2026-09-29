/* Runs in <head>, before the first paint, so a saved theme never flashes
 * the default colours. Two independent choices, both kept per browser:
 *   data-theme  the family (none = Default), from the Theme menu;
 *   data-mode   "light" or "dark", from the mode button: Auto follows the
 *               system setting and changes with it, Light and Dark pin it.
 *
 * Choices are kept in cookies, not only localStorage: localStorage belongs to
 * one origin, and every viewer runs on its own port (and, in a container, its
 * own host name), so a choice saved there was gone the next time. A cookie
 * ignores the port, and is set on the widest parent domain the browser
 * accepts (orb.local for <container>.orb.local), so every viewer sees it.
 * window.ReviewPrefs exposes the same store to app.js (panel widths).
 * The page works the same when storage is unavailable. */
(function () {
  const THEME_KEY = "review-viewer.theme";
  const MODE_KEY = "review-viewer.mode";
  const THEMES = [
    ["", "Default"], ["phosphor", "Phosphor"], ["amber", "Amber"],
    ["turbo", "Turbo"], ["onebit", "1-bit"], ["gameboy", "Game Boy"], ["solarized", "Solarized"],
  ];
  const MODES = { auto: "◐ Auto", light: "☀ Light", dark: "☾ Dark" };
  const NEXT = { auto: "light", light: "dark", dark: "auto" };
  const YEAR = 60 * 60 * 24 * 365;
  const root = document.documentElement;
  const system = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function cookieName(key) {
    return key.replace(/[^A-Za-z0-9_-]/g, "_");
  }

  function readCookie(key) {
    const name = cookieName(key) + "=";
    try {
      for (const part of document.cookie.split(";")) {
        const c = part.trim();
        if (c.startsWith(name)) return decodeURIComponent(c.slice(name.length));
      }
    } catch (err) { /* cookies blocked */ }
    return null;
  }

  /* Parent domains of the page's host, widest first, never a bare TLD:
   * a.b.orb.local → ["orb.local", "b.orb.local"]. None for an IP address or
   * a single-label name such as localhost. */
  function parentDomains(host) {
    if (!host || /^[\d.]+$/.test(host) || host.includes(":")) return [];
    const labels = host.split(".");
    const out = [];
    for (let i = labels.length - 2; i >= 1; i--) out.push(labels.slice(i).join("."));
    return out;
  }

  function writeCookie(key, value) {
    const base = cookieName(key) + "=" + encodeURIComponent(value) + "; path=/; max-age=" + YEAR + "; SameSite=Lax";
    try {
      // The browser silently refuses a domain it treats as public; the first
      // one that reads back is the widest it allows.
      for (const domain of parentDomains(location.hostname)) {
        document.cookie = base + "; domain=" + domain;
        if (readCookie(key) === value) return;
      }
      document.cookie = base;
    } catch (err) { /* cookies blocked */ }
  }

  function load(key) {
    const fromCookie = readCookie(key);
    if (fromCookie != null) return fromCookie;
    try { return localStorage.getItem(key) || ""; } catch (err) { return ""; }
  }

  function save(key, value) {
    writeCookie(key, value);
    try { localStorage.setItem(key, value); } catch (err) { /* private window */ }
  }

  window.ReviewPrefs = { load: load, save: save };

  let theme = load(THEME_KEY);
  if (!THEMES.some((t) => t[0] === theme)) theme = "";
  let mode = load(MODE_KEY);
  if (!(mode in MODES)) mode = "auto";

  function applyTheme() {
    if (theme) root.setAttribute("data-theme", theme);
    else root.removeAttribute("data-theme");
  }

  /* The highlight.js sheets are chosen by media query; point both at the
   * resolved mode instead, so Light on a dark system gets the light one. */
  function applyMode() {
    const dark = mode === "dark" || (mode === "auto" && !!system && system.matches);
    root.setAttribute("data-mode", dark ? "dark" : "light");
    for (const link of document.querySelectorAll('link[rel="stylesheet"][href*="highlight.js"]')) {
      const darkSheet = link.href.includes("github-dark");
      link.media = darkSheet === dark ? "all" : "not all";
    }
    const btn = document.getElementById("mode-btn");
    if (btn) {
      btn.textContent = MODES[mode];
      btn.title = "Light or dark: " + (mode === "auto" ? "following the system setting" : "fixed") +
        ". Click for " + MODES[NEXT[mode]].slice(2) + ".";
    }
  }

  applyTheme();
  applyMode();
  if (system) {
    const onSystem = () => { if (mode === "auto") applyMode(); };
    if (system.addEventListener) system.addEventListener("change", onSystem);
    else if (system.addListener) system.addListener(onSystem);
  }

  document.addEventListener("DOMContentLoaded", () => {
    applyMode();  // the stylesheet links and the button exist now
    const sel = document.getElementById("theme-select");
    if (sel) {
      for (const [value, label] of THEMES) {
        const opt = document.createElement("option");
        opt.value = value;
        opt.textContent = label;
        sel.appendChild(opt);
      }
      sel.value = theme;
      sel.addEventListener("change", () => { theme = sel.value; save(THEME_KEY, theme); applyTheme(); });
    }
    const btn = document.getElementById("mode-btn");
    if (btn) {
      btn.addEventListener("click", () => { mode = NEXT[mode]; save(MODE_KEY, mode); applyMode(); });
    }
  });
})();
