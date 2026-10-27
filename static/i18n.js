// Sinag Jobs languages: English (en), Tagalog (tl), Cebuano (ceb).
// Pages are written in English. When another language is chosen, this swaps visible text,
// placeholders and labels for their translations as the page renders, including text added later
// (dialogs, messages, notices). Anything without a translation stays in English.
// Translations live in /i18n/tl.json and /i18n/ceb.json, keyed by the English text.
(function () {
  const LANGS = { en: "English", tl: "Tagalog", ceb: "Cebuano" };
  const KEY = "sinag_lang";
  const get = () => { try { return localStorage.getItem(KEY); } catch (e) { return null; } };
  const set = v => { try { localStorage.setItem(KEY, v); } catch (e) { /* private mode */ } };
  const nav = (navigator.language || "").toLowerCase();
  let lang = get() || (nav.startsWith("fil") || nav.startsWith("tl") ? "tl" : nav.startsWith("ceb") ? "ceb" : "en");
  if (!LANGS[lang]) lang = "en";
  const collect = /[?&]i18n_collect=1/.test(location.search) || window.__I18N_COLLECT;
  const seen = new Set(), miss = new Set(), outs = new Set();   // outs: text we produced (never re-translated)
  let dict = null, patterns = [];
  const SKIP = new Set(["SCRIPT", "STYLE", "CODE", "PRE", "TEXTAREA", "NOSCRIPT", "SVG", "TIME"]);
  const ATTRS = ["placeholder", "aria-label", "title", "alt"];
  const norm = s => s.replace(/\s+/g, " ").trim();

  function tr(text) {
    const k = norm(text);
    if (!k || !/[A-Za-z]/.test(k) || outs.has(k)) return null;
    if (collect) seen.add(k);
    if (!dict) return null;
    if (dict[k]) { outs.add(dict[k]); return dict[k]; }
    for (const [re, out] of patterns) {
      const m = re.exec(k);
      if (m) {
        const r = out.replace(/\$(\d)/g, (_, i) => tr(m[+i]) || m[+i]);
        if (r !== k) { outs.add(r); return r; }
      }
    }
    if (collect) miss.add(k);
    return null;
  }
  function skip(el) {
    for (let e = el; e && e !== document.body; e = e.parentElement) {
      if (SKIP.has(e.tagName) || e.getAttribute("translate") === "no" || e.isContentEditable || e.classList.contains("brand")) return true;
    }
    return false;
  }
  function textNode(n) {
    if (!n.parentElement || skip(n.parentElement)) return;
    const v = n.nodeValue, t = tr(v);
    if (t && norm(v) !== t) {
      const lead = v.match(/^\s*/)[0], trail = v.match(/\s*$/)[0];
      n.nodeValue = lead + t + trail;
    }
  }
  function element(el) {
    if (skip(el)) return;
    for (const a of ATTRS) {
      const v = el.getAttribute && el.getAttribute(a);
      if (v) { const t = tr(v); if (t && t !== v) el.setAttribute(a, t); }
    }
    if (el.tagName === "INPUT" && /^(submit|button)$/.test(el.type) && el.value) { const t = tr(el.value); if (t) el.value = t; }
  }
  function walk(root) {
    if (root.nodeType === 3) return textNode(root);
    if (root.nodeType !== 1) return;
    element(root);
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
    for (let n = w.nextNode(); n; n = w.nextNode()) n.nodeType === 3 ? textNode(n) : element(n);
  }
  function start() {
    walk(document.body);
    if (document.title) { const t = tr(document.title.split(" · ")[0]); if (t) document.title = t + " · Sinag Jobs"; }
    new MutationObserver(muts => {
      for (const m of muts) {
        if (m.type === "characterData") textNode(m.target);
        else if (m.type === "attributes") element(m.target);
        else m.addedNodes.forEach(walk);
      }
    }).observe(document.body, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ATTRS });
  }

  // Language picker in the header
  function picker() {
    const nav = document.querySelector("header.top nav.links");
    if (!nav || nav.querySelector(".langsel")) return;
    const sel = document.createElement("select");
    sel.className = "langsel"; sel.setAttribute("aria-label", "Language"); sel.setAttribute("translate", "no");
    // Short codes on phones so the header fits; full names on wider screens.
    const short = window.matchMedia("(max-width: 640px)").matches;
    sel.innerHTML = Object.entries(LANGS).map(([k, v]) => `<option value="${k}"${k === lang ? " selected" : ""}>${short ? { en: "EN", tl: "TL", ceb: "CEB" }[k] : v}</option>`).join("");
    sel.title = "Language / Wika / Pinulongan";
    sel.onchange = async () => {
      set(sel.value);
      try {
        await fetch("/api/me/lang", { method: "PUT", credentials: "same-origin", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ lang: sel.value }) });
      } catch (e) { /* signed out: the browser remembers it */ }
      location.reload();
    };
    nav.insertBefore(sel, nav.firstChild);
  }

  window.I18N = {
    lang: () => lang, langs: LANGS, t: s => tr(s) || s,
    missing: () => [...miss].sort(),
    seen: () => [...seen].sort(),
    // Signed-in people get the language they picked on another device
    adopt(user) {
      if (user && user.lang && user.lang !== lang && !get()) { set(user.lang); location.reload(); }
    },
  };
  document.documentElement.lang = lang === "en" ? "en" : lang === "tl" ? "tl" : "ceb";

  const ready = fn => document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", fn) : fn();
  if (lang === "en" && !collect) { ready(picker); return; }
  const load = lang === "en" ? Promise.resolve(null)
    : fetch(`/i18n/${lang}.json`, { cache: "force-cache" }).then(r => r.ok ? r.json() : null).catch(() => null);
  // Hide text briefly so English doesn't flash before the translation (max 1.5 s)
  if (lang !== "en") {
    const st = document.createElement("style"); st.id = "i18n-hide"; st.textContent = "body{visibility:hidden}";
    document.head.appendChild(st);
    setTimeout(() => st.remove(), 1500);
  }
  load.then(data => {
    if (data) {
      dict = data.strings || {};
      patterns = (data.patterns || []).map(([re, out]) => [new RegExp("^" + re + "$"), out]);
    }
    ready(() => { start(); picker(); const h = document.getElementById("i18n-hide"); if (h) h.remove(); });
  });
})();
