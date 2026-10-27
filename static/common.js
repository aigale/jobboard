// Shared helpers for every Sinag Jobs page.
(function () {
  const SPRITE = `<svg width="0" height="0" style="position:absolute" aria-hidden="true" id="sprite"><defs>
    <g id="ray"><polygon points="0,-20 -3.4,-44 0,-56 3.4,-44"/><polygon points="-5,-20 -9,-40 -8,-46 -6,-40" transform="rotate(-9)"/><polygon points="5,-20 6,-40 8,-46 9,-40" transform="rotate(9)"/></g>
    <polygon id="star5" points="0,-10 2.35,-3.24 9.51,-3.09 3.8,1.24 5.88,8.09 0,4 -5.88,8.09 -3.8,1.24 -9.51,-3.09 -2.35,-3.24"/>
    <symbol id="i-pin" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="2" d="M12 21s7-6.2 7-12a7 7 0 1 0-14 0c0 5.8 7 12 7 12z"/><circle cx="12" cy="9" r="2.5" fill="none" stroke="currentColor" stroke-width="2"/></symbol>
    <symbol id="i-clock" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2"/><path d="M12 7v5l3 2" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></symbol>
    <symbol id="i-search" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7" fill="none" stroke="currentColor" stroke-width="2"/><path d="m20 20-3.5-3.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></symbol>
    <symbol id="i-check" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10" fill="#FCD116"/><path d="m7.5 12.5 3 3 6-6.5" fill="none" stroke="#0038A8" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></symbol>
    <symbol id="i-heart" viewBox="0 0 24 24"><path d="M12 20s-7-4.4-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.6-7 10-7 10z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/></symbol>
    <symbol id="i-bell" viewBox="0 0 24 24"><path d="M6 16V11a6 6 0 1 1 12 0v5l2 2H4z M10 20a2 2 0 0 0 4 0" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/></symbol>
    <symbol id="i-file" viewBox="0 0 24 24"><path d="M6 3h8l4 4v14H6z M14 3v4h4" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/></symbol>
  </defs></svg>`;
  if (!document.getElementById("sprite") && !document.querySelector("svg defs #ray")) {
    document.body.insertAdjacentHTML("afterbegin", SPRITE);
  }

  const COLORS = ["#0038A8", "#CE1126", "#8A6B00", "#1C5FD6", "#8E0C1B", "#2B3A67", "#0F7C6E", "#6A3FB5"];
  const S = {
    locale: () => (window.I18N && I18N.lang() === "tl") ? "fil-PH" : "en-PH",
    esc: s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])),
    peso: n => "₱" + Number(n).toLocaleString(S.locale()),
    colorFor: s => COLORS[[...String(s)].reduce((a, c) => a + c.charCodeAt(0), 0) % COLORS.length],
    initials: s => String(s).split(/[\s.&]+/).filter(Boolean).slice(0, 2).map(w => w[0]).join("").toUpperCase(),
    ago(iso) {
      const m = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
      if (m < 60) return `${m} min ago`;
      const h = Math.round(m / 60); if (h < 24) return `${h} hr${h > 1 ? "s" : ""} ago`;
      const d = Math.round(h / 24); return `${d} day${d > 1 ? "s" : ""} ago`;
    },
    date: iso => iso ? new Date(iso).toLocaleString(S.locale(), { dateStyle: "medium", timeStyle: "short" }) : "",
    day: iso => iso ? new Date(iso).toLocaleDateString(S.locale(), { dateStyle: "medium" }) : "",
    statusPill: label => `<span class="status s-${String(label).toLowerCase().replace(/[^a-z]+/g, "-")}">${S.esc(label)}</span>`,
    size: b => b > 1048576 ? (b / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(b / 1024)) + " KB",

    async api(path, opts = {}) {
      const init = { method: opts.method || "GET", credentials: "same-origin", headers: {} };
      if (opts.body instanceof FormData) init.body = opts.body;
      else if (opts.body !== undefined) { init.body = JSON.stringify(opts.body); init.headers["Content-Type"] = "application/json"; }
      let r;
      try { r = await fetch("/api" + path, init); }
      catch (e) { throw new Error("Can't reach the server. Check your connection and try again."); }
      let data = null;
      try { data = await r.json(); } catch (e) { /* not JSON */ }
      if (!r.ok) {
        let msg = data && data.detail, extra = null;
        if (Array.isArray(msg)) msg = msg.map(d => String(d.msg || "").replace(/^Value error, /, "")).join(" ");
        else if (msg && typeof msg === "object") { extra = msg; msg = msg.message; }
        const err = new Error(msg || `Something went wrong (error ${r.status}). Try again.`);
        err.status = r.status; err.data = extra;
        throw err;
      }
      return data;
    },

    _me: null,
    me(refresh) {
      if (!S._me || refresh) S._me = S.api("/auth/me").then(d => { if (window.I18N) I18N.adopt(d.user); return d.user; }).catch(() => null);
      return S._me;
    },

    toast(msg) {
      let t = document.getElementById("toast");
      if (!t) { t = document.createElement("div"); t.id = "toast"; t.className = "toast"; t.setAttribute("role", "status"); document.body.appendChild(t); }
      t.textContent = msg; t.hidden = false;
      clearTimeout(S._tt); S._tt = setTimeout(() => (t.hidden = true), 3500);
    },

    home(user) {
      if (!user) return "/";
      return { seeker: "/seeker.html", employer: "/employer.html", admin: "/admin.html" }[user.role] || "/";
    },

    async renderAccount(el) {
      if (!el) return;
      const user = await S.me();
      if (!user) {
        el.innerHTML = `<a class="plain" href="/login.html">Sign in</a>
          <a class="btn btn-red hide-xs" href="/login.html?role=employer#signup">Post a job</a>`;
        return;
      }
      let unread = 0;
      try { unread = (await S.api("/me/notifications")).unread; } catch (e) {}
      const links = {
        seeker: [["/seeker.html#applications", "My applications"], ["/seeker.html#matches", "Jobs for you"],
                 ["/seeker.html#profile", "Profile and resume"], ["/seeker.html#alerts", "Job alerts"]],
        employer: [["/employer.html#jobs", "My job posts"], ["/employer.html#new", "Post a job"], ["/employer.html#company", "Company profile"]],
        admin: [["/admin.html", "Admin"]],
      }[user.role] || [];
      const notifLink = S.home(user) + "#notifications";
      el.innerHTML = `
        <a class="bell" href="${notifLink}" aria-label="Notifications${unread ? `, ${unread} unread` : ""}"><svg aria-hidden="true"><use href="#i-bell"/></svg>${unread ? `<span class="dot">${unread > 99 ? "99+" : unread}</span>` : ""}</a>
        <div class="menu">
          <button type="button" aria-haspopup="true" aria-expanded="false" id="acctBtn"><span class="avatar" style="background:${S.colorFor(user.name)}">${S.esc(S.initials(user.name))}</span><span class="nm">${S.esc(user.name.split(" ")[0])}</span></button>
          <div class="drop" id="acctDrop" hidden>
            <small>${S.esc(user.email)}</small>
            ${links.map(([h, t]) => `<a href="${h}">${t}</a>`).join("")}
            <a href="/">Browse jobs</a>
            <button type="button" id="signOut">Sign out</button>
          </div>
        </div>`;
      const btn = el.querySelector("#acctBtn"), drop = el.querySelector("#acctDrop");
      btn.onclick = e => { e.stopPropagation(); drop.hidden = !drop.hidden; btn.setAttribute("aria-expanded", String(!drop.hidden)); };
      document.addEventListener("click", e => { if (!drop.hidden && !el.contains(e.target)) { drop.hidden = true; btn.setAttribute("aria-expanded", "false"); } });
      el.querySelector("#signOut").onclick = async () => {
        if (S.native()) await S.nativePushOff().catch(() => {});      // this phone stops getting your notifications
        await S.api("/auth/logout", { method: "POST" }).catch(() => {}); location.href = "/";
      };
    },

    // Sends people who aren't signed in (or have the wrong account type) to the right place.
    async requireRole(role) {
      const user = await S.me();
      if (!user) { location.href = "/login.html?next=" + encodeURIComponent(location.pathname + location.hash) + (role === "employer" ? "&role=employer" : ""); return null; }
      S._role = user.role;
      if (user.role !== role) {
        document.querySelector("main").innerHTML = `<div class="wrap page"><div class="notice warn">This page is for ${role === "seeker" ? "job seeker" : role} accounts. You're signed in as ${S.esc(user.email)}. <a href="${S.home(user)}">Go to your dashboard</a>.</div></div>`;
        return null;
      }
      return user;
    },

    tagInput(el, values, placeholder) {
      const state = { values: [...(values || [])], drawing: false };
      const draw = () => {
        state.drawing = true;   // replacing the input fires "blur"; ignore it while redrawing
        el.innerHTML = state.values.map((v, i) => `<span class="tag">${S.esc(v)}<button type="button" data-i="${i}" aria-label="Remove ${S.esc(v)}">×</button></span>`).join("") +
          `<input type="text" placeholder="${S.esc(placeholder || "Type and press Enter")}" aria-label="${S.esc(placeholder || "Add item")}">`;
        state.drawing = false;
        const inp = el.querySelector("input");
        inp.addEventListener("keydown", e => {
          if ((e.key === "Enter" || e.key === ",") && inp.value.trim()) { e.preventDefault(); add(inp.value); }
          else if (e.key === "Backspace" && !inp.value && state.values.length) { state.values.pop(); draw(); el.querySelector("input").focus(); }
        });
        inp.addEventListener("blur", () => { if (!state.drawing && inp.value.trim()) add(inp.value, true); });
      };
      const add = (raw, keepFocus) => {
        raw.split(",").map(x => x.trim()).filter(Boolean).forEach(v => {
          if (!state.values.some(x => x.toLowerCase() === v.toLowerCase())) state.values.push(v.slice(0, 60));
        });
        draw(); if (!keepFocus) el.querySelector("input").focus();
      };
      el.classList.add("tagbox");
      el.addEventListener("click", e => {
        const b = e.target.closest("button[data-i]");
        if (b) { state.values.splice(+b.dataset.i, 1); draw(); }
        else if (e.target === el) el.querySelector("input").focus();
      });
      draw();
      return { get: () => { const inp = el.querySelector("input"); if (inp && inp.value.trim()) add(inp.value, true); return [...state.values]; } };
    },

    tabs(root, onShow) {
      const buttons = [...root.querySelectorAll("[role=tab]")];
      const show = (name, push) => {
        const b = buttons.find(x => x.dataset.tab === name) || buttons[0];
        buttons.forEach(x => {
          const on = x === b;
          x.setAttribute("aria-selected", String(on)); x.tabIndex = on ? 0 : -1;
          const panel = document.getElementById(x.getAttribute("aria-controls"));
          if (panel) panel.hidden = true;
        });
        const panel = document.getElementById(b.getAttribute("aria-controls"));
        if (panel) { panel.hidden = false; panel.setAttribute("aria-labelledby", b.id); }
        if (push) history.replaceState(null, "", "#" + b.dataset.tab);
        onShow && onShow(b.dataset.tab);
      };
      buttons.forEach(b => b.addEventListener("click", () => show(b.dataset.tab, true)));
      root.addEventListener("keydown", e => {
        const i = buttons.findIndex(b => b.getAttribute("aria-selected") === "true");
        if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
          const n = buttons[(i + (e.key === "ArrowRight" ? 1 : buttons.length - 1)) % buttons.length];
          n.focus(); show(n.dataset.tab, true);
        }
      });
      return show;
    },

    // "Confirm your email" banner with a resend button. Also says thanks after the link is clicked.
    verifyBanner(el, user) {
      if (!el || !user) return;
      if (new URLSearchParams(location.search).get("verified") === "1") {
        S.toast("Email confirmed. Thanks!");
        history.replaceState(null, "", location.pathname + location.hash);
      }
      if (user.email_verified) return;
      el.insertAdjacentHTML("afterbegin", `<div class="notice warn" id="verifyNotice" style="margin-bottom:18px;display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between">
        <span><b>Confirm your email.</b> We sent a link to ${S.esc(user.email)}. You need it to ${user.role === "employer" ? "publish jobs" : "apply and get alerts"}.</span>
        <button class="btn btn-ghost btn-sm" type="button" id="resendVerify">Send a new link</button></div>`);
      document.getElementById("resendVerify").onclick = async e => {
        e.target.disabled = true;
        try { S.toast((await S.api("/auth/resend-verification", { method: "POST" })).message); }
        catch (err) { S.toast(err.message); e.target.disabled = false; }
      };
    },

    // Browser push on/off switch. Renders nothing useful if the browser or the site can't do push.
    async pushControl(el) {
      if (!el) return;
      if (S.native()) return S.nativePushControl(el);
      const supported = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
      let cfg = { enabled: false };
      try { cfg = await S.api("/push/key"); } catch (e) {}
      if (!supported) { el.innerHTML = `<p class="hint">This browser doesn't support push notifications. On iPhone, add Sinag Jobs to your Home Screen first (Share, then Add to Home Screen).</p>`; return; }
      if (!cfg.enabled) { el.innerHTML = `<p class="hint">Push notifications aren't switched on for this site yet. You'll still get emails and notifications here.</p>`; return; }
      const reg = await navigator.serviceWorker.register("/sw.js");
      const draw = async () => {
        const sub = await reg.pushManager.getSubscription();
        const blocked = Notification.permission === "denied";
        el.innerHTML = `<div style="display:flex;flex-wrap:wrap;gap:12px;align-items:center">
          <span>${sub ? "Push notifications are <b>on</b> for this browser." : "Get a phone or desktop notification the moment something happens."}</span>
          <button class="btn ${sub ? "btn-ghost" : "btn-blue"} btn-sm" type="button" id="pushBtn"${blocked ? " disabled" : ""}>${sub ? "Turn off" : "Turn on push notifications"}</button></div>
          ${blocked ? `<p class="hint" style="margin:8px 0 0">Notifications are blocked for this site in your browser settings. Allow them there, then reload.</p>` : ""}`;
        document.getElementById("pushBtn").onclick = async () => {
          try {
            if (sub) {
              await S.api("/me/push/off", { method: "POST", body: { endpoint: sub.endpoint } }).catch(() => {});
              await sub.unsubscribe(); S.toast("Push notifications turned off");
            } else {
              if (await Notification.requestPermission() !== "granted") { S.toast("Allow notifications in the browser prompt to turn them on."); return draw(); }
              const key = Uint8Array.from(atob(cfg.public_key.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(cfg.public_key.length / 4) * 4, "=")), c => c.charCodeAt(0));
              const s = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
              S.toast((await S.api("/me/push", { method: "POST", body: s.toJSON() })).message);
            }
          } catch (err) { S.toast(err.message || "Couldn't change push notifications."); }
          draw();
        };
      };
      draw();
    },

    // The test-payment warning. One wording everywhere, so people learn to recognise it.
    TEST_NOTICE: "This is a test environment. Do not enter real payment details.",
    testBanner(el) { if (el) el.innerHTML = `<div class="testmode" role="note">${S.esc(S.TEST_NOTICE)}</div>`; },

    // A small line chart: shared y-axis from 0, light grid, hover tooltip, legend for 2+ series, table view.
    lineChart(el, { title, sub, days, series, unit }) {
      const W = 640, H = 220, L = 40, R = 16, T = 12, B = 28;
      const max = Math.max(1, ...series.flatMap(s => s.values));
      const step = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000, 10000].find(x => max / x <= 5) || Math.ceil(max / 5);
      const top = Math.ceil(max / step) * step, ticks = [];
      for (let v = 0; v <= top; v += step) ticks.push(v);
      const x = i => L + (days.length === 1 ? 0 : i * (W - L - R) / (days.length - 1));
      const y = v => T + (H - T - B) * (1 - v / top);
      const fmtDay = d => new Date(d + "T00:00:00").toLocaleDateString(S.locale(), { month: "short", day: "numeric" });
      const xIdx = [0, Math.floor((days.length - 1) / 2), days.length - 1].filter((v, i, a) => a.indexOf(v) === i);
      const path = vals => vals.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
      const last = days.length - 1;
      el.classList.add("chart");
      el.innerHTML = `<h3>${S.esc(title)}</h3>${sub ? `<p class="sub">${S.esc(sub)}</p>` : ""}
        ${series.length > 1 ? `<div class="legend">${series.map(s => `<span><i style="background:${s.color}"></i>${S.esc(s.name)}</span>`).join("")}</div>` : ""}
        <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${S.esc(title)}">
          ${ticks.map(t => `<line x1="${L}" x2="${W - R}" y1="${y(t)}" y2="${y(t)}" stroke="var(--line)" stroke-width="1"/>
            <text x="${L - 8}" y="${y(t) + 4}" text-anchor="end" font-size="11" fill="var(--muted)">${t.toLocaleString(S.locale())}</text>`).join("")}
          ${xIdx.map(i => `<text x="${x(i)}" y="${H - 8}" text-anchor="${i === 0 ? "start" : i === last ? "end" : "middle"}" font-size="11" fill="var(--muted)">${fmtDay(days[i])}</text>`).join("")}
          ${series.map(s => `<path d="${path(s.values)}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
            <circle cx="${x(last)}" cy="${y(s.values[last])}" r="4" fill="${s.color}" stroke="var(--surface)" stroke-width="2"/>`).join("")}
          <line class="xh" x1="0" x2="0" y1="${T}" y2="${H - B}" stroke="var(--muted)" stroke-width="1" stroke-dasharray="3 3" visibility="hidden"/>
          <rect x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="transparent" class="hit"/>
        </svg>
        <div class="tip" hidden></div>
        <p class="sub" style="margin:4px 0 0;font-size:12px">The last point is today, so far.</p><details><summary>Show as a table</summary><div class="table-wrap" style="margin-top:8px;max-height:240px;overflow:auto"><table>
          <thead><tr><th>Day</th>${series.map(s => `<th class="n">${S.esc(s.name)}</th>`).join("")}</tr></thead>
          <tbody>${days.map((d, i) => `<tr><td>${fmtDay(d)}</td>${series.map(s => `<td class="n">${s.values[i].toLocaleString(S.locale())}</td>`).join("")}</tr>`).reverse().join("")}</tbody></table></div></details>`;
      const svg = el.querySelector("svg"), tip = el.querySelector(".tip"), xh = el.querySelector(".xh");
      const move = ev => {
        const box = svg.getBoundingClientRect(), px = (ev.clientX - box.left) * W / box.width;
        const i = Math.max(0, Math.min(last, Math.round((px - L) / ((W - L - R) / Math.max(1, last)))));
        xh.setAttribute("x1", x(i)); xh.setAttribute("x2", x(i)); xh.setAttribute("visibility", "visible");
        tip.hidden = false;
        tip.innerHTML = `${fmtDay(days[i])}${i === last ? " (today, so far)" : ""}<br>` + series.map(s => `<span style="display:inline-block;width:10px;height:3px;background:${s.color};vertical-align:middle;margin-right:6px"></span>${S.esc(s.name)} <b>${s.values[i].toLocaleString(S.locale())}</b>`).join("<br>");
        const left = (x(i) / W) * box.width + svg.offsetLeft;
        tip.style.left = Math.min(left + 12, el.clientWidth - tip.offsetWidth - 8) + "px"; tip.style.top = (svg.offsetTop + 8) + "px";
      };
      const hide = () => { tip.hidden = true; xh.setAttribute("visibility", "hidden"); };
      const hit = el.querySelector(".hit");
      hit.addEventListener("pointermove", move); hit.addEventListener("pointerleave", hide);
    },

    // ---------- Messages: a two-pane inbox (thread list + conversation), shared by job seekers and employers
    async inbox(el, openId) {
      const esc = S.esc;
      el.innerHTML = `<div class="inbox"><div class="tlist" role="list"></div><div class="conv"><div class="empty-sm" style="margin:auto">Pick a conversation.</div></div></div>`;
      const box = el.firstElementChild, list = box.querySelector(".tlist"), conv = box.querySelector(".conv");
      let current = null, timer = null;
      const drawList = async () => {
        const d = await S.api("/messages/threads");
        list.innerHTML = d.threads.length ? d.threads.map(t => `<button type="button" role="listitem" data-app="${t.application_id}" aria-current="${t.application_id === current}">
            ${t.unread ? `<span class="unread" aria-label="${t.unread} unread">${t.unread}</span>` : ""}
            <b>${esc(t.candidate || t.company)}</b><small>${esc(t.job)}${t.candidate ? "" : ""}</small>
            <small>${t.last.mine ? "You: " : ""}${esc(t.last.body)}</small></button>`).join("")
          : `<div class="empty-sm">No messages yet.${S._role === "seeker" ? " Employers can message you after you apply." : " Open an applicant and choose Message to start."}</div>`;
        return d;
      };
      const open = async id => {
        current = id; box.classList.add("open");
        list.querySelectorAll("button").forEach(b => b.setAttribute("aria-current", String(+b.dataset.app === id)));
        const t = await S.api(`/messages/${id}`);
        const fmt = iso => new Date(iso).toLocaleString(S.locale(), { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
        conv.innerHTML = `<header><button class="btn btn-ghost btn-sm back" type="button" style="margin-bottom:6px">← All messages</button>
            <b>${esc(t.me === "employer" ? t.candidate : t.company)}</b><small>${esc(t.job.title)}</small></header>
          <div class="msgs" aria-live="polite">${t.messages.length ? t.messages.map(m => `<div class="msg ${m.side === "system" ? "system" : m.mine ? "mine" : "theirs"}">${m.side !== "system" && !m.mine ? `<span class="who">${esc(m.sender)}</span>` : ""}${esc(m.body)}<time datetime="${m.at}">${fmt(m.at)}${m.mine && m.read ? " · Seen" : ""}</time></div>`).join("")
            : `<div class="empty-sm" style="margin:auto">${t.me === "employer" ? "Say hello. The applicant gets a notification and an email." : "No messages yet."}</div>`}</div>
          ${t.can_send ? `<form class="composer"><label class="sr-only" for="msgBody">Message</label><textarea class="input" id="msgBody" maxlength="4000" placeholder="Write a message" required></textarea><button class="btn btn-blue">Send</button></form>`
            : `<p class="hint" style="padding:12px 16px;margin:0;border-top:1px solid var(--line)">${t.me === "candidate" ? "You can reply once the employer sends you a message." : "This conversation is closed."}</p>`}`;
        const msgs = conv.querySelector(".msgs"); msgs.scrollTop = msgs.scrollHeight;
        conv.querySelector(".back").onclick = () => { box.classList.remove("open"); current = null; history.replaceState(null, "", "#messages"); };
        const form = conv.querySelector("form");
        if (form) {
          const ta = form.querySelector("textarea");
          ta.addEventListener("keydown", e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) form.requestSubmit(); });
          form.addEventListener("submit", async e => {
            e.preventDefault(); const btn = form.querySelector("button"); btn.disabled = true;
            try { await S.api(`/messages/${id}`, { method: "POST", body: { body: ta.value } }); await open(id); drawList(); }
            catch (err) { S.toast(err.message); btn.disabled = false; }
          });
        }
        history.replaceState(null, "", "#messages-" + id);
        drawList();
      };
      list.addEventListener("click", e => { const b = e.target.closest("[data-app]"); if (b) open(+b.dataset.app).catch(err => S.toast(err.message)); });
      const d = await drawList();
      if (openId) await open(openId);
      else if (d.threads.length && window.innerWidth > 720) await open(d.threads[0].application_id);
      clearInterval(S._inboxTimer);
      S._inboxTimer = setInterval(() => {            // light polling while the tab is visible
        if (document.hidden || !el.isConnected || el.closest("[hidden]")) return;
        if (current && !(conv.querySelector("textarea") || {}).value) open(current).catch(() => {}); else drawList().catch(() => {});
      }, 20000);
    },

    async unreadMessages(el) {
      try { const d = await S.api("/messages/threads"); el.textContent = d.unread || ""; } catch (e) {}
    },

    // ---------- Interviews
    interviewCard(iv, viewer) {
      const esc = S.esc;
      const status = { proposed: viewer === "candidate" ? "Pick a time" : "Waiting for the applicant", confirmed: "Confirmed",
        declined: viewer === "candidate" ? "You asked for other times" : "Asked for other times", cancelled: "Cancelled" }[iv.status];
      const link = /^https?:\/\//.test(iv.location) ? `<a href="${esc(iv.location)}" target="_blank" rel="noopener nofollow">${esc(iv.location)}</a>` : esc(iv.location);
      return `<div class="rowcard" data-iv="${iv.id}"><div style="min-width:0">
          <h3>${esc(viewer === "employer" ? iv.candidate : iv.company)} ${S.statusPill(status)}</h3>
          <div class="meta"><span>${esc(iv.job.title)}</span><span>${esc(iv.mode_label)}, ${iv.duration_minutes} min</span>${iv.location ? `<span>${link}</span>` : ""}</div>
          ${iv.status === "confirmed" ? `<p style="margin:8px 0 0;font-weight:700">${esc(iv.when)}</p>` : ""}
          ${iv.status === "proposed" && viewer === "candidate" ? `<form class="pick"><div class="slots" role="radiogroup" aria-label="Interview times">${iv.slots.map((s, i) =>
              `<label><input type="radio" name="slot-${iv.id}" value="${esc(s.at)}"${i === 0 ? " checked" : ""}><span>${esc(s.label)}</span></label>`).join("")}</div>
              <div class="actions"><button class="btn btn-red btn-sm" data-accept="${iv.id}">Confirm this time</button><button class="btn btn-ghost btn-sm" type="button" data-other="${iv.id}">None of these work</button></div></form>`
            : iv.status === "proposed" ? `<p class="hint" style="margin:8px 0 0">Offered: ${iv.slots.map(s => esc(s.label)).join("; ")}</p>` : ""}
          ${iv.notes ? `<p class="prose hint" style="margin:8px 0 0">${esc(iv.notes)}</p>` : ""}
          ${iv.cancel_reason ? `<p class="hint" style="margin:8px 0 0">Reason: ${esc(iv.cancel_reason)}</p>` : ""}
        </div><div class="side">
          ${iv.ics_url ? `<a class="btn btn-blue btn-sm" href="${iv.ics_url}">Add to calendar (.ics)</a><a class="btn btn-ghost btn-sm" href="${esc(iv.google_url)}" target="_blank" rel="noopener">Google Calendar</a>` : ""}
          ${viewer === "employer" && ["proposed", "declined", "confirmed"].includes(iv.status) ? `<button class="btn btn-ghost btn-sm" data-resched="${iv.id}">New times</button>` : ""}
          ${["proposed", "confirmed", "declined"].includes(iv.status) ? `<button class="linkbtn" data-cancel="${iv.id}">Cancel interview</button>` : ""}
          <a class="linkbtn" href="#messages-${iv.application_id}">Messages</a>
        </div></div>`;
    },

    // Three date-time pickers for offering interview times (Philippine time).
    slotInputs(prefix) {
      const d = new Date(Date.now() + 86400000); d.setMinutes(0, 0, 0); d.setHours(10);
      const v = x => { const y = new Date(x); y.setMinutes(y.getMinutes() - y.getTimezoneOffset()); return y.toISOString().slice(0, 16); };
      return [0, 1, 2].map(i => `<label class="f"><span>Time ${i + 1}${i ? " (optional)" : ""}</span><input class="input" type="datetime-local" id="${prefix}${i}" ${i ? "" : `value="${v(d)}" required`}></label>`).join("");
    },
    readSlots(prefix) {
      // Times are entered in Philippine time (UTC+8) whatever the computer's clock says.
      return [0, 1, 2].map(i => document.getElementById(prefix + i).value).filter(Boolean).map(v => v + ":00+08:00");
    },

    // ---------- Inside the Android / iPhone app (Capacitor). On the normal website these do nothing.
    native: () => !!(window.Capacitor && window.Capacitor.isNativePlatform && window.Capacitor.isNativePlatform()),
    _plugin: name => window.Capacitor && window.Capacitor.Plugins && window.Capacitor.Plugins[name],
    async nativePushOn(ask) {
      const FM = S._plugin("FirebaseMessaging"); if (!FM) return false;
      let perm = await FM.checkPermissions();
      if (perm.receive !== "granted" && ask) perm = await FM.requestPermissions();
      if (perm.receive !== "granted") return false;
      const { token } = await FM.getToken();
      await S.api("/me/devices", { method: "POST", body: { token, platform: window.Capacitor.getPlatform() } });
      return true;
    },
    async nativePushOff() {
      const FM = S._plugin("FirebaseMessaging"); if (!FM) return;
      const { token } = await FM.getToken();
      await S.api("/me/devices/off", { method: "POST", body: { token } });
    },
    async nativePushControl(el) {
      const FM = S._plugin("FirebaseMessaging");
      if (!FM) { el.innerHTML = `<p class="hint">Update the app to get notifications.</p>`; return; }
      const draw = async () => {
        const on = (await FM.checkPermissions()).receive === "granted";
        el.innerHTML = `<div style="display:flex;flex-wrap:wrap;gap:12px;align-items:center">
          <span>${on ? "Notifications are on for this phone." : "Get a notification on this phone the moment something happens."}</span>
          ${on ? "" : `<button class="btn btn-blue btn-sm" type="button" id="pushBtn">Turn on notifications</button>`}</div>
          ${on ? `<p class="hint" style="margin:8px 0 0">To turn them off, use your phone's Settings for Sinag Jobs.</p>` : ""}`;
        const b = document.getElementById("pushBtn");
        if (b) b.onclick = async () => {
          try { S.toast(await S.nativePushOn(true) ? "Notifications are on." : "Allow notifications for Sinag Jobs in your phone's Settings."); }
          catch (err) { S.toast(err.message || "Couldn't turn on notifications."); }
          draw();
        };
      };
      draw();
    },
    _nativeSetup() {
      if (!S.native() || S._nativeDone) return;
      S._nativeDone = true;
      document.documentElement.classList.add("in-app");
      const App = S._plugin("App"), FM = S._plugin("FirebaseMessaging"), Browser = S._plugin("Browser");
      if (App) {
        App.addListener("backButton", ({ canGoBack }) => canGoBack ? history.back() : App.exitApp());
        // Social sign-in came back from the phone's browser: swap the one-time code for a session here
        App.addListener("appUrlOpen", ({ url }) => {
          const m = /:\/\/auth\?code=([\w-]+)/.exec(url || "");
          let v = null; try { v = sessionStorage.getItem("sinag_app_verifier"); } catch (e) {}
          if (m && v) { if (Browser) Browser.close().catch(() => {}); location.href = `/api/auth/app-login?code=${m[1]}&verifier=${encodeURIComponent(v)}`; }
        });
      }
      if (FM) {
        FM.addListener("notificationActionPerformed", e => { const u = e && e.notification && e.notification.data && e.notification.data.url; if (u && u.startsWith("/")) location.href = u; });
        FM.addListener("tokenReceived", ({ token }) => { S.me().then(u => u && S.api("/me/devices", { method: "POST", body: { token, platform: window.Capacitor.getPlatform() } })).catch(() => {}); });
        S.me().then(u => { if (u) S.nativePushOn(false).catch(() => {}); });     // keep this phone registered
      }
    },
    // Opens social sign-in in the phone's browser (Google and Facebook block sign-in inside apps).
    async nativeSocial(href) {
      const bytes = crypto.getRandomValues(new Uint8Array(48));
      const verifier = btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
      const hash = new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
      const challenge = btoa(String.fromCharCode(...hash)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
      try { sessionStorage.setItem("sinag_app_verifier", verifier); } catch (e) {}
      const url = new URL(href, location.origin); url.searchParams.set("app_challenge", challenge);
      const Browser = S._plugin("Browser");
      if (Browser) await Browser.open({ url: url.toString() }); else location.href = url.toString();
    },

    async notificationsPanel(el) {
      const d = await S.api("/me/notifications");
      el.innerHTML = d.items.length ? `<div class="rows">${d.items.map(n => `
        <div class="rowcard"><div><b style="font-weight:${n.read ? 500 : 800}">${S.esc(n.text)}</b><div class="meta"><span>${S.ago(n.at)}</span></div></div>
        <div class="side">${n.link ? `<a class="btn btn-ghost btn-sm" href="${S.esc(n.link)}">Open</a>` : ""}</div></div>`).join("")}</div>`
        : `<div class="empty-sm">No notifications yet. Updates on your applications and account show up here.</div>`;
      if (d.unread) { await S.api("/me/notifications/read", { method: "POST" }).catch(() => {}); const dot = document.querySelector(".bell .dot"); if (dot) dot.remove(); }
    },
  };
  window.S = S;
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => S._nativeSetup()); else S._nativeSetup();
})();
