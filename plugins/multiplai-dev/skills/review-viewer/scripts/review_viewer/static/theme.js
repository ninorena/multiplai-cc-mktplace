/* Runs in <head>, before the first paint, so a saved theme never flashes
 * the default colours. Two independent choices, both kept per browser:
 *   data-theme  the family (none = Default), from the Theme menu;
 *   data-mode   "light" or "dark", from the mode button: Auto follows the
 *               system setting and changes with it, Light and Dark pin it.
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
  const root = document.documentElement;
  const system = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function load(key) {
    try { return localStorage.getItem(key) || ""; } catch (err) { return ""; }
  }
  function save(key, value) {
    try { localStorage.setItem(key, value); } catch (err) { /* private window */ }
  }

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
