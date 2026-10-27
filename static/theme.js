// Sinag Jobs light and dark mode.
// Switching themes changes colours, backgrounds, borders, shadows and form-control
// colours only. Nothing here rewrites, replaces, hides or distorts page text, images,
// icons or layout, and images are never filtered. The choice is saved per browser;
// until someone picks one, the site follows the operating system.
(function () {
  const KEY = "sinag_theme";
  const MODES = ["light", "dark"];
  const root = document.documentElement;
  const query = window.matchMedia("(prefers-color-scheme: dark)");
  const get = () => { try { const v = localStorage.getItem(KEY); return MODES.includes(v) ? v : null; } catch (e) { return null; } };
  const set = v => { try { localStorage.setItem(KEY, v); } catch (e) { /* private mode */ } };
  const system = () => query.matches ? "dark" : "light";
  let mode = get() || system();

  function paint(next) {
    mode = next;
    // Explicit light or dark, so a choice wins over the operating system in both directions.
    root.setAttribute("data-theme", mode);
    root.style.colorScheme = mode;
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", mode === "dark" ? "#0A1226" : "#0038A8");
    const group = document.querySelector(".themegroup");
    if (group) {
      for (const b of group.querySelectorAll("button")) b.setAttribute("aria-pressed", String(b.dataset.themeSet === mode));
    }
  }

  // Two fixed options, both always visible: the labels themselves never change with the theme.
  function control() {
    const nav = document.querySelector("header.top nav.links");
    if (!nav || nav.querySelector(".themegroup")) return;
    const group = document.createElement("div");
    group.className = "themegroup";
    group.setAttribute("role", "group");
    group.setAttribute("aria-label", "Colour theme");
    group.setAttribute("translate", "no");
    for (const m of MODES) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "themebtn";
      b.dataset.themeSet = m;
      b.textContent = m === "light" ? "Light" : "Dark";
      b.setAttribute("aria-label", m === "light" ? "Light mode" : "Dark mode");
      b.setAttribute("aria-pressed", String(m === mode));
      b.addEventListener("click", () => { set(m); paint(m); });
      group.appendChild(b);
    }
    const lang = nav.querySelector(".langsel");
    nav.insertBefore(group, lang ? lang.nextSibling : nav.firstChild);
  }

  const ready = fn => document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", fn) : fn();
  paint(mode);
  ready(control);

  // Keep following the system only while no choice has been saved.
  const followSystem = e => { if (!get()) paint(e.matches ? "dark" : "light"); };
  if (query.addEventListener) query.addEventListener("change", followSystem);
  else if (query.addListener) query.addListener(followSystem);

  // Another tab changed the theme.
  window.addEventListener("storage", e => { if (e.key === KEY) paint(get() || system()); });
})();
