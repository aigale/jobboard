// Sinag Jobs board: search, filters, job details and Easy Apply.
const { esc, peso, ago } = S;
const $ = id => document.getElementById(id);
const PAGE_SIZE = 25;
const PAY_STEPS = [15000, 20000, 25000, 30000, 40000, 50000, 60000, 80000, 100000, 150000];

let saved = new Set();
try { saved = new Set(JSON.parse(localStorage.getItem("sinag-saved") || "[]")); } catch (e) {}
const persist = () => { try { localStorage.setItem("sinag-saved", JSON.stringify([...saved])); } catch (e) {} };

const state = { q: "", loc: "", cat: "", setup: new Set(), job_type: new Set(), shift: new Set(), level: new Set(),
                industry: new Set(), perk: new Set(), minPay: 0, maxPay: 0, easy: false, sort: "new", savedOnly: false, page: 1 };
const FILTERS = [["fSetup", "setup"], ["fType", "job_type"], ["fShift", "shift"], ["fLevel", "level"], ["fIndustry", "industry"], ["fPerks", "perk"]];
let facets = null, lastJobs = [], me = null;

const isNew = iso => Date.now() - new Date(iso).getTime() < 3600e3;
const pay = j => j.salary_max ? `${peso(j.salary_min)}–${peso(j.salary_max)}<small> /mo</small>` : `<small>Salary not stated</small>`;

function buildChecks(el, key, list) {
  el.innerHTML = list.length ? list.map((f, i) => `<label class="opt"><input type="checkbox" id="${key}-${i}" value="${esc(f.value)}"${state[key].has(f.value) ? " checked" : ""}> ${esc(f.value)} <small>${f.count}</small></label>`).join("")
    : `<span class="range-out" style="color:var(--muted)">None yet</span>`;
}
function renderFacets() {
  buildChecks($("fSetup"), "setup", facets.setup);
  buildChecks($("fType"), "job_type", facets.job_type || []);
  buildChecks($("fShift"), "shift", facets.shift);
  buildChecks($("fLevel"), "level", facets.level);
  buildChecks($("fIndustry"), "industry", (facets.industry || []).slice(0, 10));
  buildChecks($("fPerks"), "perk", facets.perk.slice(0, 8));
  const chips = [{ value: "", count: facets.total_open, label: "All" }, ...facets.category];
  $("cats").innerHTML = chips.map(c => `<button type="button" class="chip" aria-pressed="${state.cat === c.value}" data-cat="${esc(c.value)}">${esc(c.label || c.value)}<small>${c.count}</small></button>`).join("");
  const locSel = $("loc"), cur = locSel.value;
  locSel.innerHTML = `<option value="">All of the Philippines</option>` + facets.location.map(l => `<option value="${esc(l.value)}">${esc(l.value)} (${l.count})</option>`).join("");
  locSel.value = cur;
  $("alertCat").innerHTML = `<option value="">Any category</option>` + facets.category.map(c => `<option>${esc(c.value)}</option>`).join("");
  $("statJobs").textContent = facets.total_open.toLocaleString(S.locale());
  $("statCompanies").textContent = facets.companies.toLocaleString(S.locale());
  $("statUpdated").textContent = facets.last_crawl_at ? ago(facets.last_crawl_at) : "Not yet";
  $("easyCount").textContent = facets.easy_apply ? facets.easy_apply : "";
}
for (const [id, key] of FILTERS) {
  $(id).addEventListener("change", e => { const s = state[key]; e.target.checked ? s.add(e.target.value) : s.delete(e.target.value); reload(); });
}
const payOpts = PAY_STEPS.map(v => `<option value="${v}">${peso(v)}</option>`).join("");
$("minPay").insertAdjacentHTML("beforeend", payOpts);
$("maxPay").insertAdjacentHTML("beforeend", payOpts);
$("cats").addEventListener("click", e => {
  const b = e.target.closest(".chip"); if (!b) return;
  state.cat = b.dataset.cat;
  document.querySelectorAll("#cats .chip").forEach(c => c.setAttribute("aria-pressed", c.dataset.cat === state.cat));
  reload();
});

// Privacy-friendly counting: no cookies, no IDs. The server turns these into daily totals.
function track(body) {
  try { fetch("/api/track", { method: "POST", keepalive: true, credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).catch(() => {}); } catch (e) {}
}
let lastTracked = "", searchTimer;
function trackSearch(now) {
  clearTimeout(searchTimer);
  const go = () => { const q = state.q.trim().toLowerCase(); if (q.length >= 2 && q !== lastTracked) { lastTracked = q; track({ type: "search", q }); } };
  now ? go() : (searchTimer = setTimeout(go, 1500));
}

function query() {
  const p = new URLSearchParams();
  if (state.q) p.set("q", state.q);
  if (state.loc) p.set("location", state.loc);
  if (state.cat) p.set("category", state.cat);
  for (const [, k] of FILTERS) state[k].forEach(v => p.append(k, v));
  if (state.minPay) p.set("min_pay", state.minPay);
  if (state.maxPay) p.set("max_pay", state.maxPay);
  if (state.easy) p.set("easy_apply", "true");
  if (state.savedOnly) p.set("ids", [...saved].join(",") || "0");
  p.set("sort", state.sort); p.set("page", state.page); p.set("page_size", PAGE_SIZE);
  return "?" + p.toString();
}

function card(j) {
  return `<article class="job${j.featured ? " featured" : ""}" data-id="${j.id}" tabindex="0" aria-label="${esc(j.title)} at ${esc(j.company.name)}">
    <div class="logo" style="background:${j.company.logo_url ? "var(--surface)" : S.colorFor(j.company.name)}">${j.company.logo_url ? `<img src="${esc(j.company.logo_url)}" alt="" style="width:100%;height:100%;object-fit:cover;border-radius:12px">` : esc(S.initials(j.company.name))}</div>
    <div style="min-width:0">
      <h3>${esc(j.title)}</h3>
      <div class="co">${esc(j.company.name)}</div>
      <div class="meta">
        <span><svg aria-hidden="true"><use href="#i-pin"/></svg>${esc(j.city && j.setup !== "Remote" ? j.city : j.location)}</span>
        <span class="${j.setup === "Remote" ? "pill remote" : "pill"}">${esc(j.setup)}</span>
        ${j.job_type && j.job_type !== "Full-time" && j.job_type !== "Not stated" ? `<span class="pill">${esc(j.job_type)}</span>` : ""}
        ${j.shift === "Night shift" ? '<span class="pill night">Night shift</span>' : ""}
        ${j.featured ? '<span class="pill feat">Featured</span>' : ""}
        ${j.easy_apply ? '<span class="pill easy">Easy Apply</span>' : ""}
        ${isNew(j.posted_at) ? '<span class="pill new">New</span>' : ""}
        <span><svg aria-hidden="true"><use href="#i-clock"/></svg>${ago(j.posted_at)}</span>
      </div>
    </div>
    <div class="right">
      <div class="salary">${pay(j)}</div>
      <button class="save" type="button" aria-pressed="${saved.has(j.id)}" aria-label="Save job" data-save="${j.id}"><svg aria-hidden="true"><use href="#i-heart"/></svg></button>
    </div>
  </article>`;
}

let reqId = 0;
async function loadJobs(append = false) {
  const my = ++reqId;
  if (!append) $("resultCount").textContent = "Loading jobs…";
  try {
    const data = await S.api("/jobs" + query());
    if (my !== reqId) return;
    lastJobs = append ? lastJobs.concat(data.jobs) : data.jobs;
    $("jobs").innerHTML = lastJobs.length ? lastJobs.map(card).join("") : `<div class="empty"><strong>No jobs match these filters yet.</strong><br>Try removing a filter, or set up a job alert and we'll email you when one lands.</div>`;
    $("resultCount").textContent = `${data.total.toLocaleString(S.locale())} open job${data.total === 1 ? "" : "s"}${state.loc ? " in " + state.loc : ""}`;
    $("more").hidden = lastJobs.length >= data.total;
  } catch (err) {
    if (my !== reqId) return;
    $("jobs").innerHTML = `<div class="empty"><strong>Jobs couldn't load.</strong><br>${esc(err.message)}</div>`;
    $("resultCount").textContent = ""; $("more").hidden = true;
  }
}
let t; function reload() { state.page = 1; clearTimeout(t); t = setTimeout(() => loadJobs(false), 150); }
$("more").addEventListener("click", () => { state.page++; loadJobs(true); });

$("jobs").addEventListener("click", e => {
  const s = e.target.closest("[data-save]");
  if (s) { const id = +s.dataset.save; saved.has(id) ? saved.delete(id) : saved.add(id); persist(); s.setAttribute("aria-pressed", saved.has(id)); S.toast(saved.has(id) ? "Job saved" : "Removed from saved jobs"); if (state.savedOnly) reload(); return; }
  const c = e.target.closest(".job"); if (c) openJob(+c.dataset.id);
});
$("jobs").addEventListener("keydown", e => { if (e.key === "Enter" && e.target.classList.contains("job")) openJob(+e.target.dataset.id); });

// ---------------------------------------------------------------- Job details
// Some cached/older copies of index.html (or a browser extension) may be missing the
// modal markup. Create it on demand so one missing element cannot stop the whole board
// from initialising.
function ensureModal() {
  let scrim = $("scrim"), modal = $("modal");
  if (!scrim) {
    scrim = document.createElement("div");
    scrim.id = "scrim";
    scrim.className = "scrim";
    scrim.hidden = true;
    document.body.appendChild(scrim);
  }
  if (!modal) {
    modal = document.createElement("div");
    modal.id = "modal";
    modal.className = "modal";
    modal.setAttribute("role", "dialog");
    modal.setAttribute("aria-modal", "true");
    modal.setAttribute("aria-labelledby", "mTitle");
  }
  if (modal.parentElement !== scrim) scrim.appendChild(modal);
  return scrim;
}
const scrim = ensureModal();
let current = null;
async function openJob(id) {
  $("modal").innerHTML = `<div class="mbody">Loading…</div>`; scrim.hidden = false;
  if (location.hash !== "#job-" + id) history.replaceState(null, "", "#job-" + id);
  let j;
  try { j = await S.api("/jobs/" + id); }
  catch (err) { $("modal").innerHTML = `<div class="mbody"><p>${esc(err.message)}</p><button class="btn btn-ghost" id="mClose">Close</button></div>`; $("mClose").onclick = closeJob; return; }
  current = j;
  const applied = j.my_application;
  const applyBtn = j.easy_apply
    ? (applied && applied.stage !== "withdrawn"
        ? `<a class="btn btn-ghost" href="/seeker.html#applications">Applied · ${esc(applied.status)}</a>`
        : `<button class="btn btn-red" type="button" id="mEasy">Easy Apply</button>`)
    : `<a class="btn btn-red" href="${esc(j.apply_url)}" target="_blank" rel="noopener" id="mExt">Apply on company site</a>`;
  $("modal").innerHTML = `
    <div class="mhead"><div class="logo" style="background:${S.colorFor(j.company.name)}">${esc(S.initials(j.company.name))}</div>
      <div><h2 id="mTitle">${esc(j.title)}</h2><div class="co" style="color:var(--muted);font-weight:600;margin-top:4px">${esc(j.company.name)} · ${esc(j.location_raw || j.location)}</div></div>
      <button class="x" type="button" id="mClose" aria-label="Close">×</button></div>
    <div class="mbody" id="mBody">
      ${j.match ? `<div class="notice good" style="margin-bottom:14px"><span class="match">Your match <b>${j.match.score}%</b></span>${j.match.reasons.length ? `<ul class="reasons">${j.match.reasons.map(r => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}</div>` : ""}
      <div class="facts">
        <div><small>Monthly pay</small><b>${j.salary_max ? `${peso(j.salary_min)}–${peso(j.salary_max)}` : "Not stated"}</b></div>
        <div><small>Work setup</small><b>${esc(j.setup)}</b></div>
        <div><small>Job type</small><b>${esc(j.job_type)}</b></div>
        <div><small>Schedule</small><b>${esc(j.shift)}</b></div>
        <div><small>Experience</small><b>${esc(j.level)}</b></div>
        <div><small>Posted</small><b>${ago(j.posted_at)}</b></div>
      </div>
      ${j.salary_original && !/^PHP/.test(j.salary_original) ? `<p style="font-size:13px;color:var(--muted);margin:10px 0 0">Published as ${esc(j.salary_original)}; peso amount is an estimate.</p>` : ""}
      ${j.skills && j.skills.length ? `<h4>Skills</h4><div style="display:flex;flex-wrap:wrap;gap:6px">${j.skills.map(p => `<span class="pill">${esc(p)}</span>`).join("")}</div>` : ""}
      ${j.perks.length ? `<h4>Benefits</h4><div style="display:flex;flex-wrap:wrap;gap:6px">${j.perks.map(p => `<span class="pill">${esc(p)}</span>`).join("")}</div>` : ""}
      <h4>About the role</h4>
      <div class="desc">${j.description_html || "<p>See the full description on the company's site.</p>"}</div>
      ${j.company_about ? `<h4>About ${esc(j.company.name)}</h4><p class="prose" style="margin:0">${esc(j.company_about)}</p>` : ""}
      ${j.has_company_page ? `<p style="margin:10px 0 0"><a href="/company.html?c=${encodeURIComponent(j.company.slug)}">See ${esc(j.company.name)}'s company page</a></p>` : ""}
      ${j.easy_apply && (j.has_test || j.has_chat) ? `<p class="hint" style="margin:12px 0 0">After you apply, ${esc(j.company.name)} may ask you to ${[j.has_test && "take a short skills test", j.has_chat && "answer a few follow-up questions in a chat"].filter(Boolean).join(" and ")}.</p>` : ""}
    </div>
    <div class="mfoot" id="mFoot">
      ${applyBtn}
      <button class="btn btn-ghost" type="button" id="mSave">${saved.has(j.id) ? "Saved" : "Save job"}</button>
      ${j.page_url ? `<a class="btn btn-ghost" href="${esc(j.page_url)}">Full page</a>` : ""}
    </div>`;
  $("mClose").focus(); $("mClose").onclick = closeJob;
  $("mSave").onclick = () => {
    saved.has(j.id) ? saved.delete(j.id) : saved.add(j.id); persist(); $("mSave").textContent = saved.has(j.id) ? "Saved" : "Save job";
    const b = document.querySelector(`[data-save="${j.id}"]`); if (b) b.setAttribute("aria-pressed", saved.has(j.id));
  };
  if ($("mEasy")) $("mEasy").onclick = () => startApply(j);
  if ($("mExt")) $("mExt").addEventListener("click", () => track({ type: "apply_click", job_id: j.id }));
}
function closeJob() { scrim.hidden = true; current = null; if (/^#job-/.test(location.hash)) history.replaceState(null, "", location.pathname + location.search); }
scrim.addEventListener("click", e => { if (e.target.id === "scrim") closeJob(); });
document.addEventListener("keydown", e => { if (e.key === "Escape" && !scrim.hidden) closeJob(); });

// ---------------------------------------------------------------- Easy Apply
async function startApply(j) {
  if (!me) { location.href = "/login.html?next=" + encodeURIComponent("/#job-" + j.id) + "#signup"; return; }
  if (me.role !== "seeker") { S.toast("Sign in with a job seeker account to apply."); return; }
  $("mBody").innerHTML = `<p>Loading your profile…</p>`; $("mFoot").innerHTML = "";
  let profile, docs;
  try { [profile, docs] = await Promise.all([S.api("/me/profile"), S.api("/me/documents")]); }
  catch (err) { $("mBody").innerHTML = `<p class="err">${esc(err.message)}</p>`; return; }
  if (!profile.headline) {
    $("mBody").innerHTML = `<div class="notice warn">Add a headline and your skills to your profile first. Employers see your profile with every application.</div>`;
    $("mFoot").innerHTML = `<a class="btn btn-red" href="/seeker.html#profile">Complete my profile</a><button class="btn btn-ghost" id="mBack" type="button">Back</button>`;
    $("mBack").onclick = () => openJob(j.id); return;
  }
  const firstResume = docs.find(d => d.kind === "resume");
  const docList = () => docs.length ? docs.map(d => `<label class="doc"><input type="checkbox" name="doc" value="${d.id}"${d.id === (firstResume && firstResume.id) ? " checked" : ""}>
      <span class="ico" style="background:${d.kind === "resume" ? "var(--flag-red)" : "var(--flag-blue)"}">${esc(d.filename.split(".").pop().toUpperCase())}</span>
      <span class="nm"><b>${esc(d.filename)}</b><small>${esc(d.kind)} · ${S.size(d.size)}</small></span></label>`).join("")
    : `<p class="hint">No files yet. Upload your resume below.</p>`;
  const qs = j.questions.map(q => {
    const req = q.required ? " *" : "";
    let input;
    if (q.kind === "yesno") input = `<div class="checks">${["Yes", "No"].map(o => `<label class="check"><input type="radio" name="q${q.id}" value="${o}"> ${o}</label>`).join("")}</div>`;
    else if (q.kind === "choice") input = `<select id="q${q.id}"><option value="">Choose…</option>${q.options.map(o => `<option>${esc(o)}</option>`).join("")}</select>`;
    else input = `<textarea id="q${q.id}" rows="3" maxlength="2000" style="min-height:80px"></textarea>`;
    return `<div class="f"><span>${esc(q.text)}${req}</span>${input}</div>`;
  }).join("");
  $("mBody").innerHTML = `
    <form id="applyForm" style="display:flex;flex-direction:column;gap:18px">
      <div class="notice">Applying as <b>${esc(profile.full_name)}</b>, ${esc(profile.headline)}. ${esc(j.company.name)} will see your profile and the files you pick. <a href="/seeker.html#profile" target="_blank" rel="noopener">Edit profile</a></div>
      <div class="f"><span>Files to send *</span><div class="docs" id="docList">${docList()}</div></div>
      <div class="f"><span>Upload a file</span>
        <div class="split"><select id="upKind" class="input"><option value="resume">Resume</option><option value="certificate">Certificate</option><option value="portfolio">Portfolio</option></select>
        <input type="file" id="upFile" accept=".pdf,.docx,.png,.jpg,.jpeg" class="input"></div>
        <small>PDF, Word, PNG or JPG, up to 5 MB.</small></div>
      ${qs ? `<div style="display:flex;flex-direction:column;gap:14px"><h4 style="margin:0">Questions from ${esc(j.company.name)}</h4>${qs}</div>` : ""}
      <label class="f"><span>Cover letter (optional)</span><textarea id="cover" rows="5" maxlength="5000" placeholder="A few lines on why you're a good fit."></textarea></label>
      ${profile.priority_credits ? `<label class="check"><input type="checkbox" id="prio"> Send as a priority application (${profile.priority_credits} left). ${esc(j.company.name)} sees it at the top of the list.</label>` : ""}
      <p class="err" id="applyErr" hidden></p>
    </form>`;
  $("mFoot").innerHTML = `<button class="btn btn-red" type="button" id="mSend">Send application</button><button class="btn btn-ghost" type="button" id="mBack">Back</button>`;
  $("mBack").onclick = () => openJob(j.id);
  $("upFile").onchange = async e => {
    const f = e.target.files[0]; if (!f) return;
    const fd = new FormData(); fd.append("file", f); fd.append("kind", $("upKind").value);
    $("upFile").disabled = true;
    try {
      const d = await S.api("/me/documents", { method: "POST", body: fd });
      const checked = new Set([...document.querySelectorAll("input[name=doc]:checked")].map(x => +x.value));
      docs.unshift(d); $("docList").innerHTML = docList();
      checked.add(d.id); document.querySelectorAll("input[name=doc]").forEach(x => (x.checked = checked.has(+x.value)));
      S.toast("Uploaded " + d.filename);
    } catch (err) { S.toast(err.message); }
    finally { $("upFile").disabled = false; $("upFile").value = ""; }
  };
  $("mSend").onclick = async () => {
    const answers = {};
    for (const q of j.questions) {
      const v = q.kind === "yesno" ? (document.querySelector(`input[name=q${q.id}]:checked`) || {}).value || "" : $("q" + q.id).value;
      answers[q.id] = v;
    }
    const body = { document_ids: [...document.querySelectorAll("input[name=doc]:checked")].map(x => +x.value), cover_letter: $("cover").value, answers,
      priority: !!($("prio") && $("prio").checked) };
    $("mSend").disabled = true; $("applyErr").hidden = true;
    try {
      const r = await S.api(`/jobs/${j.id}/apply`, { method: "POST", body });
      $("mBody").innerHTML = `<div class="notice good"><b>${esc(r.message)}</b><br>You'll get an email and a notification when ${esc(j.company.name)} updates your application.</div>`;
      $("mFoot").innerHTML = `<a class="btn btn-blue" href="/seeker.html#applications">Track my applications</a><button class="btn btn-ghost" type="button" id="mDone">Keep browsing</button>`;
      $("mDone").onclick = closeJob;
    } catch (err) { $("applyErr").textContent = err.message; $("applyErr").hidden = false; $("mSend").disabled = false; }
  };
}

// ---------------------------------------------------------------- Search, filters, alerts
$("heroSearch").addEventListener("submit", e => { e.preventDefault(); state.q = $("q").value.trim(); state.loc = $("loc").value; reload(); trackSearch(true); $("board").scrollIntoView({ behavior: "smooth" }); });
$("q").addEventListener("input", e => { state.q = e.target.value.trim(); reload(); trackSearch(false); });
$("loc").addEventListener("change", e => { state.loc = e.target.value; reload(); });
$("sort").addEventListener("change", e => { state.sort = e.target.value; reload(); });
$("minPay").addEventListener("change", e => { state.minPay = +e.target.value; reload(); });
$("maxPay").addEventListener("change", e => { state.maxPay = +e.target.value; reload(); });
$("easyOnly").addEventListener("change", e => { state.easy = e.target.checked; reload(); });
$("savedOnly").addEventListener("change", e => { state.savedOnly = e.target.checked; reload(); });
$("reset").addEventListener("click", () => {
  Object.assign(state, { q: "", loc: "", cat: "", minPay: 0, maxPay: 0, easy: false, savedOnly: false });
  FILTERS.forEach(([, k]) => state[k].clear());
  document.querySelectorAll(".filters input[type=checkbox]").forEach(c => (c.checked = false));
  $("q").value = ""; $("loc").value = ""; $("minPay").value = "0"; $("maxPay").value = "0";
  document.querySelectorAll("#cats .chip").forEach(c => c.setAttribute("aria-pressed", c.dataset.cat === ""));
  reload();
});
document.querySelectorAll("footer [data-loc],footer [data-q]").forEach(a => a.addEventListener("click", () => {
  if (a.dataset.loc) { state.loc = a.dataset.loc; $("loc").value = a.dataset.loc; state.q = ""; $("q").value = ""; }
  if (a.dataset.q) { state.q = a.dataset.q; $("q").value = a.dataset.q; state.loc = ""; $("loc").value = ""; }
  reload();
}));
$("alertForm").addEventListener("submit", async e => {
  e.preventDefault();
  const btn = e.target.querySelector("button"); btn.disabled = true;
  try {
    let msg;
    if (me) {
      await S.api("/me/alerts", { method: "POST", body: { category: $("alertCat").value || null, location: state.loc || null, frequency: "daily" } });
      msg = "Alert saved. Manage it under Job alerts in your account.";
    } else {
      msg = (await S.api("/alerts", { method: "POST", body: { email: $("alertEmail").value, category: $("alertCat").value || null, location: state.loc || null } })).message;
    }
    S.toast(msg); e.target.reset();
  } catch (err) { S.toast(err.message); }
  finally { btn.disabled = false; }
});

function openFromHash() { const m = /^#job-(\d+)$/.exec(location.hash); if (m) openJob(+m[1]); }
window.addEventListener("hashchange", openFromHash);

(async () => {
  S.renderAccount($("account"));
  track({ type: "visit" });
  me = await S.me();
  if (me) { $("alertEmail").value = me.email; $("alertEmail").readOnly = true; }
  try { facets = await S.api("/facets"); renderFacets(); } catch (e) { /* the job list shows the error */ }
  loadJobs();
  openFromHash();
})();
