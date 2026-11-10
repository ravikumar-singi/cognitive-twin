"use strict";
/* Cognitive Twin web app. Served as a separate file so the Content-Security-Policy can
   forbid inline scripts. All server text is escaped before it touches innerHTML. */

const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const LETTERS = ["A", "B", "C", "D"];
const ICON = { locked:"🔒", ready:"○", learning:"…", mastered:"✓", credentialed:"★" };
const STATUS_TEXT = { locked:"Locked", ready:"Ready", learning:"Learning", mastered:"Mastered", credentialed:"Credential earned" };

const BAND_TEXT = { under13:"Under 13", "13to17":"13 to 17", adult:"18 or over", unknown:"Not set yet" };
const S = { csrf:null, user:null, cfg:null, me:null, decision:null, answered:false, busy:false,
            breakTimer:null, demo:null, playing:false, timer:null, frame:0, speed:700,
            track:null, subjects:null, catalog:null };
const store = { get(k) { try { return localStorage.getItem(k); } catch { return null; } },
                set(k, v) { try { localStorage.setItem(k, v); } catch {} } };
/* learner calls carry the chosen subject */
const T = path => S.track ? `${path}${path.includes("?") ? "&" : "?"}track=${encodeURIComponent(S.track)}` : path;
async function catalog() { return S.catalog || (S.catalog = await api("/api/public/subjects").catch(() => [])); }
const trackName = id => (S.catalog || []).find(t => t.id === id)?.name || id;
const trackSize = id => (S.catalog || []).find(t => t.id === id)?.skills || 6;

class ApiError extends Error { constructor(msg, status, code) { super(msg); this.status = status; this.code = code; } }

async function api(path, { method = "GET", body } = {}) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && S.csrf) headers["X-CSRF-Token"] = S.csrf;
  let r;
  try { r = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), credentials:"same-origin" }); }
  catch { throw new ApiError("Can't reach the server. Check your connection and try again.", 0); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    if (r.status === 401 && S.user) { S.user = null; toast("Your session ended. Please sign in again."); boot(); }
    if (r.status === 403 && r.headers.get("X-CT-Code") === "password_change_required") { renderPassword(true); }
    throw new ApiError(typeof j.detail === "string" ? j.detail : "Something went wrong. Please try again.", r.status, j.code);
  }
  return j;
}
function toast(msg, ms = 4000) {
  const t = document.createElement("div"); t.className = "toast"; t.textContent = msg;
  $("toasts").appendChild(t); setTimeout(() => t.remove(), ms);
}
function view(html) { stopTimers(); $("view").innerHTML = html; window.scrollTo(0, 0); }
function stopTimers() { clearInterval(S.breakTimer); clearInterval(S.timer); S.playing = false; }
function on(id, ev, fn) { const el = $(id); if (el) el.addEventListener(ev, fn); }
function fmtDate(s) { return s ? new Date(s).toLocaleString() : "never"; }

/* ================= boot and sign-in ================= */
async function boot() {
  stopTimers();
  S.cfg = S.cfg || await api("/auth/config").catch(() => ({ sso:false, local_accounts:false, dev_login:false, personas:[], demo:false }));
  try {
    const s = await api("/auth/session");
    S.user = s.user; S.csrf = s.user ? (s.csrf || S.csrf) : null;
  } catch { S.user = null; }
  $("who").hidden = $("roleChip").hidden = $("signOutBtn").hidden = !S.user;
  if (!S.user) return renderSignIn();
  $("who").textContent = S.user.name;
  $("roleChip").textContent = { learner:"learner", educator:"teacher", school_admin:"school admin", admin:"platform admin" }[S.user.role] || S.user.role;
  if (S.trackUser !== S.user.id) { S.track = null; S.subjects = null; S.decision = null; S.trackUser = S.user.id; }
  if (S.user.must_change_password) return renderPassword(true);
  await catalog();
  if (S.user.role === "learner") return renderLearner();
  return renderStaff();
}

function signedIn(r) { S.csrf = r.headers.get("X-CSRF-Token"); }
async function post(path, body) {
  const r = await fetch(path, { method:"POST", headers:{ "Content-Type":"application/json", ...(S.csrf ? { "X-CSRF-Token":S.csrf } : {}) }, body:JSON.stringify(body), credentials:"same-origin" })
    .catch(() => { throw new ApiError("Can't reach the server. Check your connection and try again.", 0); });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new ApiError(typeof j.detail === "string" ? j.detail : (r.status === 422 ? "Please fill in every box." : "Something went wrong. Please try again."), r.status);
  signedIn(r); return j;
}
function pwField(id, label, hint) {
  return `<label class="field" for="${id}">${label}</label>
    <div class="pw"><input type="password" id="${id}" autocomplete="${id.includes("new") || id.includes("j") || id.includes("i") ? "new-password" : "current-password"}">
    <button type="button" class="quiet" data-show="${id}" aria-label="Show password">Show</button></div>${hint ? `<div class="small muted">${hint}</div>` : ""}`;
}
function bindShow(root = document) {
  root.querySelectorAll("[data-show]").forEach(b => b.addEventListener("click", () => {
    const i = $(b.dataset.show), show = i.type === "password";
    i.type = show ? "text" : "password"; b.textContent = show ? "Hide" : "Show"; b.setAttribute("aria-label", show ? "Hide password" : "Show password");
  }));
}

function renderSignIn(mode) {
  const c = S.cfg, local = c.local_accounts;
  mode = mode || (local ? "login" : null);
  const tab = (id, label) => `<button type="button" role="tab" data-mode="${id}" aria-selected="${mode === id}">${label}</button>`;
  const forms = {
    login: `<form id="fLogin" novalidate>
        <label class="field" for="lUser">Username</label><input type="text" id="lUser" autocomplete="username" autocapitalize="none" spellcheck="false" placeholder="for example asha-k7q2">
        ${pwField("lPass", "Password")}
        <div class="err" id="fErr" aria-live="assertive"></div>
        <button class="big" type="submit">Sign in</button>
        <p class="small muted" style="margin-top:10px">Forgot your password? Ask your teacher to reset it.</p></form>`,
    join: `<form id="fJoin" novalidate>
        <p class="small muted">Your teacher will give you a class code.</p>
        <label class="field" for="jCode">Class code</label><input type="text" id="jCode" autocomplete="off" autocapitalize="characters" spellcheck="false" placeholder="for example MATHS5A" class="code-input">
        <label class="field" for="jName">Your first name</label><input type="text" id="jName" autocomplete="given-name" maxlength="40">
        ${pwField("jPass", "Make a password", "At least 8 characters. A few words together is easy to remember, like <i>blue-tiger-runs</i>.")}
        <div class="err" id="fErr" aria-live="assertive"></div>
        <button class="big" type="submit">Join my class</button></form>`,
    invite: `<form id="fInvite" novalidate>
        <p class="small muted">For teachers and school administrators. Your school or the platform team sends you this code.</p>
        <label class="field" for="iCode">Invitation code</label><input type="text" id="iCode" autocomplete="off" autocapitalize="characters" spellcheck="false" class="code-input" placeholder="XXXX-XXXX-XXXX">
        <label class="field" for="iName">Your name</label><input type="text" id="iName" autocomplete="name" maxlength="80" placeholder="as your students know you, e.g. Ms Rao">
        ${pwField("iPass", "Make a password", "At least 10 characters.")}
        <div class="err" id="fErr" aria-live="assertive"></div>
        <button class="big" type="submit">Create my account</button></form>`,
  };
  view(`<section class="card signin">
    <h1>Welcome to Cognitive Twin</h1>
    <p class="muted">A team of AI tutors that adapts to how you learn, with credentials you can prove.</p>
    ${c.sso ? `<button class="big" id="ssoBtn" type="button">Sign in with your school</button>
      ${local ? `<p class="small muted center" style="margin:10px 0 4px">or, if your school gave you a username or code</p>` : ""}` : ""}
    ${local ? `<div class="tabs" role="tablist" aria-label="How do you want to sign in?">${tab("login", "I have a username")}${tab("join", "I'm joining a class")}${tab("invite", "I'm a teacher with a code")}</div>
      <div id="formBox">${forms[mode]}</div>` : ""}
    ${!c.sso && !local && !c.dev_login ? `<p class="banner">Sign-in isn't configured on this server yet. Ask your administrator.</p>` : ""}
    ${c.dev_login ? `<details style="margin-top:20px"${local ? "" : " open"}><summary><b>Development sign-in</b> <span class="small muted">· try any kind of person</span></summary>
      <p class="small muted">Only on development servers. Each person below is what a school's records would say about them. Demo class codes: MATHS5A, ALGEBRA9B, MLPILOTA.</p>
      <div class="persona-list">${c.personas.map(p => `<button type="button" data-persona="${esc(p.id)}">${esc(p.name)}</button>`).join("")}</div></details>` : ""}
    ${c.demo ? `<p style="margin-top:18px"><button class="secondary" type="button" id="demoBtn">Watch a demo first</button></p>` : ""}
  </section>`);
  on("ssoBtn", "click", () => { location.href = "/auth/login"; });
  on("demoBtn", "click", renderDemoSetup);
  document.querySelectorAll("[data-mode]").forEach(b => b.addEventListener("click", () => { renderSignIn(b.dataset.mode); document.querySelector(`[data-mode="${b.dataset.mode}"]`).focus(); }));
  bindShow();
  const fail = (e, btn) => { $("fErr").textContent = e.message; btn.disabled = false; };
  on("fLogin", "submit", async ev => {
    ev.preventDefault(); const btn = ev.target.querySelector("[type=submit]");
    const username = $("lUser").value.trim(), password = $("lPass").value;
    if (!username || !password) { $("fErr").textContent = "Please type your username and password."; return (username ? $("lPass") : $("lUser")).focus(); }
    btn.disabled = true;
    try { await post("/auth/local-login", { username, password }); boot(); } catch (e) { fail(e, btn); $("lPass").select(); }
  });
  on("fJoin", "submit", async ev => {
    ev.preventDefault(); const btn = ev.target.querySelector("[type=submit]");
    const code = $("jCode").value.trim(), first_name = $("jName").value.trim(), password = $("jPass").value;
    if (!code || !first_name) { $("fErr").textContent = !code ? "Please type your class code." : "Please type your first name."; return (!code ? $("jCode") : $("jName")).focus(); }
    if (password.length < 8) { $("fErr").textContent = "Your password needs at least 8 characters."; return $("jPass").focus(); }
    btn.disabled = true;
    try { const j = await post("/auth/join", { code, first_name, password }); await boot(); showUsername(j.username, j.class_name); } catch (e) { fail(e, btn); }
  });
  on("fInvite", "submit", async ev => {
    ev.preventDefault(); const btn = ev.target.querySelector("[type=submit]");
    const code = $("iCode").value.trim(), name = $("iName").value.trim(), password = $("iPass").value;
    if (!code || !name) { $("fErr").textContent = !code ? "Please type your invitation code." : "Please type your name."; return (!code ? $("iCode") : $("iName")).focus(); }
    if (password.length < 10) { $("fErr").textContent = "Your password needs at least 10 characters."; return $("iPass").focus(); }
    btn.disabled = true;
    try { const j = await post("/auth/invite", { code, name, password }); await boot(); showUsername(j.username); } catch (e) { fail(e, btn); }
  });
  document.querySelectorAll("[data-persona]").forEach(b => b.addEventListener("click", async () => {
    b.disabled = true;
    try { await post("/auth/dev-login", { persona: b.dataset.persona }); boot(); } catch { toast("Sign-in failed."); b.disabled = false; }
  }));
}

function showUsername(username, className) {
  const d = document.createElement("div");
  d.className = "card username-card"; d.setAttribute("role", "status");
  d.innerHTML = `<b>Your username is <span class="code">${esc(username)}</span></b>
    <p class="small">Write it down: you'll need it and your password to sign in next time.${className ? ` You've joined <b>${esc(className)}</b>.` : ""}</p>
    <button type="button" class="secondary" id="gotIt">I've written it down</button>`;
  $("view").prepend(d);
  on("gotIt", "click", () => d.remove());
  $("gotIt").focus();
}

function renderPassword(forced) {
  view(`<section class="card signin"><h1>${forced ? "Choose your own password" : "Change your password"}</h1>
    ${forced ? `<p class="muted">You signed in with a temporary password. Pick a new one that only you know.</p>` : ""}
    <form id="fPw" novalidate>
      ${forced ? "" : pwField("pwOld", "Current password")}
      ${pwField("pwNew", "New password", S.user && S.user.role === "learner" ? "At least 8 characters." : "At least 10 characters.")}
      ${S.user && S.user.username ? `<p class="small muted">Your username stays <b>${esc(S.user.username)}</b>.</p>` : ""}
      <div class="err" id="fErr" aria-live="assertive"></div>
      <div class="row"><button type="submit">Save password</button>${forced ? "" : `<button type="button" class="quiet" id="pwCancel">Cancel</button>`}</div>
    </form></section>`);
  bindShow();
  on("pwCancel", "click", boot);
  (forced ? $("pwNew") : $("pwOld")).focus();
  on("fPw", "submit", async ev => {
    ev.preventDefault();
    try { await post("/auth/password", { current: forced ? "" : $("pwOld").value, new: $("pwNew").value }); toast("Password saved."); boot(); }
    catch (e) { $("fErr").textContent = e.message; }
  });
}

async function signOut() {
  try { await api("/auth/logout", { method:"POST" }); } catch {}
  S.user = null; S.csrf = null; $("toasts").innerHTML = ""; boot();
}

/* ================= learner ================= */
async function renderLearner() {
  try { S.subjects = await api("/api/me/subjects"); } catch (e) { return view(`<p class="banner">${esc(e.message)}</p>`); }
  const ids = S.subjects.subjects.map(x => x.id);
  if (!ids.length) return view(`<section class="card signin"><h1>No subjects yet</h1><p>Your school hasn't switched on any subjects. Please ask your teacher.</p></section>`);
  const saved = store.get(`ct-track-${S.user.id}`);
  if (!S.track || !ids.includes(S.track)) S.track = ids.includes(saved) ? saved : null;
  if (!S.track) return ids.length === 1 ? pickSubject(ids[0]) : renderSubjects();
  try { S.me = await api(T("/api/me")); } catch (e) { return view(`<p class="banner">${esc(e.message)}</p>`); }
  const c = S.me.consent;
  if (c.not_eligible_reason) return renderWaiting(c.not_eligible_reason);
  if (!c.learning_granted) return renderConsent(c);
  renderLearning();
  if (S.me.pending) showDecision(S.me.pending); else nextStep();
}
function pickSubject(id) { S.track = id; store.set(`ct-track-${S.user.id}`, id); S.decision = null; renderLearner(); }

function renderSubjects() {
  const subs = S.subjects.subjects;
  view(`<h1 style="font-size:1.5rem">What would you like to learn?</h1>
    <p class="muted">${S.subjects.school ? esc(S.subjects.school) + " · " : ""}You can switch subjects any time. Your progress in each one is kept.</p>
    <div class="subjects">${subs.map(x => `<button type="button" class="subject" data-track="${esc(x.id)}">
      <b>${esc(x.name)}</b><span class="small muted">${esc(x.audience)}</span>
      <span class="small">${esc(x.description)}</span>
      ${x.classes.length ? `<span class="chip brand">${esc(x.classes.join(", "))}</span>` : ""}
      <span class="small muted">${x.questions_answered ? `${x.mastered} of ${x.skills} skills mastered` : `${x.skills} skills`}</span></button>`).join("")}</div>`);
  document.querySelectorAll("[data-track]").forEach(b => b.addEventListener("click", () => pickSubject(b.dataset.track)));
}

function renderWaiting(reason) {
  const u = S.user;
  view(`<section class="card signin"><h1>Almost there, ${esc(u.name.split(" ")[0])}</h1>
    <p>${esc(reason)}</p>
    ${u.username ? `<p class="small muted">Your username is <b class="code">${esc(u.username)}</b>. Keep it somewhere safe.</p>` : ""}
    <div class="row"><button type="button" id="checkAgain">Check again</button></div></section>`);
  on("checkAgain", "click", boot);
}

function renderConsent(c) {
  const child = S.user.age_band && S.user.age_band !== "adult";
  view(`<section class="card signin" aria-labelledby="ct">
    <h1 id="ct">${c.learning_needs_renewal ? "Our data policy has changed" : "Before you start"}</h1>
    ${c.learning_needs_renewal ? `<p class="banner">Please read the updated policy and agree again to keep learning.</p>` : ""}
    <div class="policy${child ? " friendly" : ""}" tabindex="0" aria-label="How we use your information">${esc(c.policy_text)}</div>
    <p class="small muted">Policy version ${esc(c.policy_version)}</p>
    <div class="switch"><input type="checkbox" id="agree"><label for="agree"><b>${child ? "I understand and I'd like to start" : "I agree to this data policy"}</b><br><span class="small muted">${child ? "If you're not sure, ask your teacher or a parent first." : "Required to use the tutors."}</span></label></div>
    ${child ? "" : `<div class="switch"><input type="checkbox" id="neural"><label for="neural"><b>Optional: I'd allow the focus headband</b><br><span class="small muted">${esc(c.neural_policy_text)}</span></label></div>`}
    <div class="err" id="consentErr" aria-live="assertive"></div>
    <button class="big" type="button" id="consentBtn">${child ? "Let's start" : "Agree and start"}</button>
  </section>`);
  on("consentBtn", "click", async () => {
    if (!$("agree").checked) { $("consentErr").textContent = "Please tick the box first, or sign out if you don't want to continue."; return $("agree").focus(); }
    $("consentBtn").disabled = true;
    try { await api(T("/api/me/consent"), { method:"POST", body:{ learning:true, neural_sensing:!!($("neural") && $("neural").checked) } }); renderLearner(); }
    catch (e) { $("consentErr").textContent = e.message; $("consentBtn").disabled = false; }
  });
}

function renderLearning() {
  view(`<div class="progress-top"><div style="flex:1;min-width:220px">
      <h1 style="font-size:1.4rem;margin:0">Hi ${esc(S.me.user.name.split(" ")[0])} 👋</h1>
      <div class="row" style="margin:4px 0 6px;gap:6px"><span class="chip brand">${esc(trackName(S.track))}</span>
        ${S.subjects && S.subjects.subjects.length > 1 ? `<button type="button" class="linkish" id="switchBtn">Change subject</button>` : ""}</div>
      <div class="small muted" id="progressText"></div>
      <div class="meter" style="margin-top:6px" role="progressbar" aria-label="Skills mastered" id="progressBar" aria-valuemin="0"><i id="progressFill" style="width:0"></i></div>
    </div></div>
    <div class="layout">
      <section class="card" id="stage" aria-live="polite"></section>
      <aside>
        <section class="card"><h2>Your learning path</h2>
          <p class="small muted">The bar shows how sure the tutors are that you know each skill. The line marks the 95% goal.</p>
          <div id="skills"></div></section>
        <section class="card" style="margin-top:16px"><h2>Your credentials</h2><div id="creds"></div></section>
        <section class="card" style="margin-top:16px"><h2>Your data</h2>
          ${S.user.age_band === "adult"
            ? `<p class="small muted">Download everything we hold about you, or delete it. Credentials you've earned stay verifiable but can no longer be linked to you.</p>`
            : `<p class="small muted">You can download everything we keep about you. To delete it, ask your teacher or a parent: they can do it straight away.</p>`}
          <div class="row"><button type="button" class="secondary" id="exportBtn">Download my data</button>
          ${S.user.age_band === "adult" ? `<button type="button" class="quiet" id="deleteBtn">Delete my data…</button>` : ""}
          ${S.user.username ? `<button type="button" class="quiet" id="pwBtn">Change password</button>` : ""}</div>
          <div id="deleteBox"></div></section>
      </aside>
      <details class="hood card" id="hood" style="margin-top:0">
        <summary>Under the hood <span class="small muted">· for the curious</span></summary>
        <h3 style="margin-top:8px">Recent decisions</h3>
        <div class="scroll"><table><thead><tr><th>#</th><th>Plan</th><th>Question</th><th>Result</th><th>Policy</th></tr></thead><tbody id="hoodLog"></tbody></table></div>
        <h3 style="margin-top:16px">Safety guardian</h3><div id="hoodGuard" class="small"></div>
      </details>
    </div>`);
  on("exportBtn", "click", exportData);
  on("deleteBtn", "click", showDelete);
  on("pwBtn", "click", () => renderPassword(false));
  on("switchBtn", "click", () => { S.track = null; store.set(`ct-track-${S.user.id}`, ""); renderSubjects(); });
  renderSide();
}

async function refreshMe() { S.me = await api(T("/api/me")); renderSide(); }

function renderSide() {
  const m = S.me, p = m.progress;
  $("progressText").textContent = `${p.mastered} of ${p.total} skills mastered · ${p.credentials} credential${p.credentials === 1 ? "" : "s"} · ${m.track}`;
  $("progressFill").style.width = `${100 * p.mastered / p.total}%`;
  $("progressBar").setAttribute("aria-valuemax", p.total); $("progressBar").setAttribute("aria-valuenow", p.mastered);
  $("skills").innerHTML = pathHtml(m.skills, "Claim credential");
  $("skills").querySelectorAll("[data-claim]").forEach(b => b.addEventListener("click", () => claim(b.dataset.claim, b)));
  $("creds").innerHTML = credsHtml(m.credentials, false);
  bindVerify($("creds"));
  $("hoodLog").innerHTML = m.log.map(e => e.action === "break"
    ? `<tr><td>${e.step}</td><td colspan="3">Pacer: break</td><td class="small">${esc(e.policy_version)}</td></tr>`
    : `<tr><td>${e.step}</td><td>${esc(skillName(e.skill))}</td><td>level ${e.difficulty} · ${esc((e.modality || "").replace(/_/g, " "))}</td><td>${e.correct === undefined ? "…" : e.correct ? "✓" : "✗"}</td><td class="small">${esc(e.policy_version)}</td></tr>`).join("") || `<tr><td colspan="5" class="muted">Nothing yet.</td></tr>`;
  $("hoodGuard").innerHTML = m.interventions.length ? m.interventions.map(i => `<div>Step ${i.step} · ${esc(i.agent)}: ${esc(i.detail)}</div>`).join("") : `<span class="muted">No interventions so far.</span>`;
}
function skillName(id) { return ((S.me && S.me.skills) || []).find(s => s.id === id)?.name || id; }

function pathHtml(skills, claimLabel) {
  return skills.map(s => `
    <div class="skill s-${s.status}">
      <div class="icon" aria-hidden="true">${ICON[s.status]}</div>
      <div>
        <div class="name">${esc(s.name)} <span class="sr">, ${STATUS_TEXT[s.status]}</span></div>
        ${s.started || s.status === "mastered" || s.status === "credentialed"
          ? `<div class="meter goal" role="img" aria-label="${s.known_pct}% sure, goal ${s.goal_pct}%"><i style="width:${s.known_pct}%"></i></div>
             <div class="small muted">${STATUS_TEXT[s.status]} · ${s.known_pct}% sure · ${esc(s.note)}</div>`
          : `<div class="small muted" style="margin-top:4px">${STATUS_TEXT[s.status]} · ${esc(s.note)}</div>`}
        ${s.can_claim && claimLabel ? `<button type="button" data-claim="${esc(s.id)}">${esc(claimLabel)}</button>` : ""}
      </div></div>`).join("");
}

function credsHtml(creds, staff) {
  if (!creds.length) return `<p class="muted small">Master a skill to claim a credential. Validators check answers (never brain data) and sign it.</p>`;
  return creds.map(c => `<div class="cred" style="${c.status === "revoked" ? "border-color:var(--bad)" : ""}">
    <b>★ ${esc(c.credentialSubject.name)}</b> ${c.status === "revoked" ? `<span class="badge no">Revoked</span>` : ""}
    <div class="small muted">Signed by ${c.proof.length} validators · ${esc(c.validFrom.slice(0, 10))}</div>
    <div class="row" style="margin-top:8px"><button type="button" class="secondary" data-verify="${esc(c.id)}">Check it's genuine</button>
    ${staff && c.status !== "revoked" ? `<button type="button" class="quiet" data-revoke="${esc(c.id)}">Revoke…</button>` : ""}</div>
    <div class="small" data-result="${esc(c.id)}" role="status"></div>
    <details class="small" style="margin-top:6px"><summary>Share or inspect</summary>
      <p>Anyone can check it at <code>/api/public/credentials/${esc(c.id)}/verify</code></p><pre>${esc(JSON.stringify(c, null, 2))}</pre></details>
  </div>`).join("");
}
function bindVerify(root) {
  root.querySelectorAll("[data-verify]").forEach(b => b.addEventListener("click", async () => {
    const out = root.querySelector(`[data-result="${CSS.escape(b.dataset.verify)}"]`);
    out.textContent = "Checking signatures and the ledger…";
    try {
      const v = await api(`/api/public/credentials/${encodeURIComponent(b.dataset.verify)}/verify`);
      out.innerHTML = v.status === "valid" ? `<span class="ok">✓ Genuine.</span> ${esc(v.reason)}.`
        : v.status === "revoked" ? `<span style="color:var(--bad)">✗ Revoked:</span> ${esc(v.reason)}`
        : `<span style="color:var(--bad)">✗ Not valid:</span> ${esc(v.reason)}`;
    } catch (e) { out.textContent = e.message; }
  }));
}

async function nextStep(skipBreak = false) {
  if (S.busy) return; S.busy = true;
  try { showDecision(await api(T("/api/me/next"), { method:"POST", body:{ skip_break: skipBreak } })); }
  catch (e) { if (e.code === "consent_required") return renderLearner(); toast(e.message); }
  finally { S.busy = false; }
}
function showDecision(d) { S.decision = d; S.answered = false; d.action === "break" ? renderBreak() : renderQuestion(); }

function whyBlock(ex) {
  if (!ex || !ex.reasons || !ex.reasons.length) return "";
  return `<details class="why"><summary>Why this question?</summary><ul>${ex.reasons.map(r => `<li>${esc(r)}</li>`).join("")}</ul></details>`;
}
function renderQuestion() {
  const d = S.decision, ex = d.explanation || {};
  $("stage").innerHTML = `
    <div class="chips"><span class="chip brand">${esc(d.item.skill_name)}</span><span class="chip">${esc(ex.difficulty_label)}</span><span class="chip">${esc(ex.modality_label)}</span></div>
    <p class="prompt" id="prompt">${esc(d.item.prompt)}</p>
    ${d.item.tip ? `<div class="tip"><b>Tip:</b> ${esc(d.item.tip)}</div>` : ""}
    <div class="options" role="group" aria-labelledby="prompt">
      ${d.item.options.map((o, i) => `<button class="opt" data-i="${i}" type="button"><kbd aria-hidden="true">${LETTERS[i]}</kbd><span>${esc(o)}</span></button>`).join("")}
    </div>
    <p class="small muted" style="margin-top:10px">Tip: press A–D or 1–4 to answer.</p>
    <div id="fb"></div>${whyBlock(ex)}`;
  $("stage").querySelectorAll(".opt").forEach(b => b.addEventListener("click", () => answer(+b.dataset.i)));
  $("stage").querySelector(".opt").focus({ preventScroll:true });
}
async function answer(i) {
  if (S.answered || S.busy) return; S.answered = true; S.busy = true;
  const btns = [...$("stage").querySelectorAll(".opt")]; btns.forEach(b => b.disabled = true);
  try {
    const r = await api(T("/api/me/answer"), { method:"POST", body:{ item_id: S.decision.item.id, choice: i } });
    btns[r.answer].classList.add("right"); if (!r.correct) btns[i].classList.add("wrong");
    $("fb").innerHTML = `<div class="feedback ${r.correct ? "good" : "bad"}" role="status">
      <b>${r.correct ? "Nice, that's right!" : `Not quite. The answer is ${LETTERS[r.answer]}.`}</b>
      ${r.correct ? "" : `<span>${esc(r.hint)}</span>`}
      <div class="small" style="margin-top:4px">The tutors are now ${Math.round(r.belief * 100)}% sure you know this skill.</div>
      <div class="row"><button type="button" id="nextBtn">Next question →</button><span class="small muted">or press Enter</span></div></div>`;
    on("nextBtn", "click", () => nextStep()); $("nextBtn").focus({ preventScroll:true });
    await refreshMe();
  } catch (e) { toast(e.message); S.answered = false; btns.forEach(b => b.disabled = false); }
  finally { S.busy = false; }
}
function renderBreak() {
  const ex = S.decision.explanation || {};
  if (S.decision.proposed_action === "done") {
    $("stage").innerHTML = `<div class="center"><h2 style="font-size:1.6rem">🎉 You've mastered every skill in ${esc(trackName(S.track))}!</h2><p class="muted">Claim your remaining credentials from your learning path${S.subjects && S.subjects.subjects.length > 1 ? ", or try another subject" : ""}.</p></div>`;
    return;
  }
  $("stage").innerHTML = `<div class="center"><h2 style="font-size:1.4rem">☕ ${esc(ex.headline || "Time for a short break")}</h2>
    <p class="muted">${(ex.reasons || []).map(esc).join(" ")}</p>
    <div class="row" style="justify-content:center"><button type="button" id="takeBtn">Take the break</button>
    <button type="button" class="secondary" id="skipBtn">Keep going</button></div></div>`;
  on("takeBtn", "click", () => startBreak(ex.break_seconds || 60));
  on("skipBtn", "click", () => nextStep(true));
  $("takeBtn").focus({ preventScroll:true });
}
function startBreak(secs) {
  let left = secs;
  $("stage").innerHTML = `<div class="center"><h2 style="font-size:1.4rem">☕ Enjoy your break</h2>
    <p class="muted">Stand up, stretch, look at something far away.</p><div class="count" id="count">${left}s</div>
    <div class="row" style="justify-content:center"><button type="button" id="backBtn">I'm back. Continue</button></div></div>`;
  clearInterval(S.breakTimer);
  S.breakTimer = setInterval(() => { left -= 1; const c = $("count"); if (c) c.textContent = left > 0 ? `${left}s` : "Ready when you are"; if (left <= 0) clearInterval(S.breakTimer); }, 1000);
  on("backBtn", "click", async () => {
    clearInterval(S.breakTimer);
    try { await api(T("/api/me/break"), { method:"POST" }); await refreshMe(); nextStep(); } catch (e) { toast(e.message); }
  });
  $("backBtn").focus({ preventScroll:true });
}
async function claim(skill, btn) {
  btn.disabled = true; btn.textContent = "Validators are checking…";
  try {
    const r = await api(T("/api/me/credentials"), { method:"POST", body:{ skill } });
    const yes = r.votes.filter(v => v.approve).length;
    toast(r.issued ? `★ Credential earned: ${yes} of ${r.votes.length} validators signed it.` : `Not yet: ${r.reason}`, 5000);
    await refreshMe();
  } catch (e) { toast(e.message); btn.disabled = false; btn.textContent = "Claim credential"; }
}
async function exportData() {
  try {
    const r = await fetch("/api/me/export", { credentials:"same-origin" });
    if (!r.ok) throw new Error();
    const url = URL.createObjectURL(await r.blob());
    const a = document.createElement("a"); a.href = url; a.download = "cognitive-twin-my-data.json";
    document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
    toast("Your data is downloading.");
  } catch { toast("Couldn't prepare your download. Please try again."); }
}
function showDelete() {
  $("deleteBox").innerHTML = `<div class="banner" style="margin-top:12px">
    <b>This permanently deletes your learning history and profile.</b> It can't be undone.
    <label class="field" for="delConfirm">Type <b>delete my data</b> to confirm</label>
    <input type="text" id="delConfirm" autocomplete="off">
    <div class="row"><button type="button" class="danger" id="delGo">Delete permanently</button><button type="button" class="quiet" id="delCancel">Cancel</button></div></div>`;
  $("delConfirm").focus();
  on("delCancel", "click", () => { $("deleteBox").innerHTML = ""; });
  on("delGo", "click", async () => {
    if ($("delConfirm").value.trim().toLowerCase() !== "delete my data") return toast('Please type "delete my data" exactly.');
    try {
      await api("/api/me/delete", { method:"POST", body:{ confirm:"delete my data" } });
      S.user = null; S.csrf = null;
      $("who").hidden = $("roleChip").hidden = $("signOutBtn").hidden = true;
      view(`<section class="card signin"><h1>Your data has been deleted</h1><p>Your profile and learning history are gone. Any credentials you earned still verify, but nothing links them to you any more.</p></section>`);
    } catch (e) { toast(e.message); }
  });
}

/* ================= teachers and administrators ================= */
const bandSelect = (id, val) => `<select id="${id}">${["under13", "13to17", "adult"].map(b => `<option value="${b}" ${b === val ? "selected" : ""}>${BAND_TEXT[b]}</option>`).join("")}</select>`;
const guardianBox = id => `<div class="switch"><input type="checkbox" id="${id}"><label for="${id}"><b>A parent or guardian has agreed</b><br><span class="small muted">Under-18s can start only once the school confirms this from its records.</span></label></div>`;
function secretBox(title, rows, note) {
  return `<div class="secret" role="status"><b>${esc(title)}</b>${rows.map(([k, v]) => `<div class="row" style="gap:8px;align-items:center"><span class="small muted">${esc(k)}</span><span class="code">${esc(v)}</span><button type="button" class="quiet copy" data-copy="${esc(v)}">Copy</button></div>`).join("")}
    ${note ? `<p class="small muted" style="margin:6px 0 0">${note}</p>` : ""}</div>`;
}
function bindCopy(root) {
  root.querySelectorAll("[data-copy]").forEach(b => b.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(b.dataset.copy); b.textContent = "Copied"; } catch { toast("Select the text and copy it."); }
  }));
}

async function renderStaff() {
  const role = S.user.role, admin = role === "admin";
  const tabs = [["classes", admin ? "All classes" : "Classes"],
    ...(role === "school_admin" ? [["school", "My school"]] : []),
    ...(admin ? [["schools", "Schools"], ["policies", "Teaching policies"], ["keys", "Signing keys"], ["ledger", "Ledger"], ["audit", "Audit log"]] : [])];
  const title = { educator:"Your classes", school_admin:esc(S.user.school || "Your school"), admin:"Platform administration" }[role];
  view(`<div class="progress-top"><h1 style="font-size:1.4rem;margin:0;flex:1">${title}</h1>
    ${S.user.username ? `<button type="button" class="quiet" id="pwBtn">Change password</button>` : ""}</div>
    <div class="tabs" role="tablist">${tabs.map(([id, label], i) => `<button type="button" role="tab" data-tab="${id}" aria-selected="${i === 0}">${label}</button>`).join("")}</div>
    <section class="card" id="panel"></section>`);
  on("pwBtn", "click", () => renderPassword(false));
  const fns = { classes: tabClasses, school: tabSchool, schools: tabSchools, policies: tabPolicies, keys: tabKeys, ledger: tabLedger, audit: tabAudit };
  document.querySelectorAll("[data-tab]").forEach(b => b.addEventListener("click", () => {
    document.querySelectorAll("[data-tab]").forEach(x => x.setAttribute("aria-selected", x === b));
    fns[b.dataset.tab]();
  }));
  tabClasses();
}
const panel = html => { $("panel").innerHTML = html; bindCopy($("panel")); bindShow($("panel")); };

async function tabClasses(selectId) {
  const classes = await api("/api/educator/cohorts").catch(e => { toast(e.message); return []; });
  const canCreate = S.user.role !== "admin";
  const subjects = (S.catalog || []);
  const newForm = canCreate ? `<details id="newClass" ${classes.length ? "" : "open"}><summary><b>+ New class</b></summary>
      <form id="fClass" class="grid2" novalidate>
        <div><label class="field" for="cName">Class name</label><input type="text" id="cName" maxlength="120" placeholder="e.g. Class 7B maths"></div>
        <div><label class="field" for="cTrack">Subject</label><select id="cTrack">${subjects.map(t => `<option value="${esc(t.id)}">${esc(t.name)} (${esc(t.audience)})</option>`).join("")}</select></div>
        <div class="err" id="cErr" aria-live="assertive"></div><div><button type="submit">Create class</button></div></form></details>` : "";
  if (!classes.length) return panel(`${newForm}<p class="muted" style="margin-top:12px">${canCreate ? "Create your first class. You'll get a code to give your students." : "No classes yet."}</p>`), bindNewClass();
  const sel = selectId && classes.some(c => c.id === selectId) ? selectId : classes[0].id;
  panel(`<div class="row" style="align-items:end"><div style="flex:1;min-width:220px"><label class="field" for="cohortSel" style="margin-top:0">Class</label>
    <select id="cohortSel">${classes.map(c => `<option value="${esc(c.id)}" ${c.id === sel ? "selected" : ""}>${esc(c.name)} · ${esc(c.track_name)} (${c.learners} learner${c.learners === 1 ? "" : "s"}${c.waiting ? `, ${c.waiting} waiting` : ""})</option>`).join("")}</select></div></div>
    ${newForm}<div id="cohortBody" style="margin-top:14px"></div>`);
  bindNewClass();
  on("cohortSel", "change", e => loadClass(classes.find(c => c.id === e.target.value)));
  await loadClass(classes.find(c => c.id === sel));
}
function bindNewClass() {
  on("fClass", "submit", async ev => {
    ev.preventDefault();
    const name = $("cName").value.trim();
    if (!name) { $("cErr").textContent = "Give the class a name."; return $("cName").focus(); }
    try { const c = await api("/api/educator/cohorts", { method:"POST", body:{ name, track:$("cTrack").value } }); toast(`Class created. Its code is ${c.join_code}.`); tabClasses(c.id); }
    catch (e) { $("cErr").textContent = e.message; }
  });
}

async function loadClass(c) {
  const rows = await api(`/api/educator/cohorts/${encodeURIComponent(c.id)}/learners`).catch(e => { toast(e.message); return []; });
  const waiting = rows.filter(r => r.waiting || (r.age_band !== "adult" && !r.guardian_ok && r.local));
  const active = rows.filter(r => !waiting.includes(r));
  const school = S.user.role !== "admin", size = trackSize(c.track);
  $("cohortBody").innerHTML = `
    <div class="joinbox">${c.join_code
      ? `<div><span class="small muted">Class code</span><div class="bigcode">${esc(c.join_code)}</div>
         <p class="small" style="margin:0">Students open this site, choose <b>I'm joining a class</b> and type the code.</p></div>
         <div class="row"><button type="button" class="secondary copy" data-copy="${esc(c.join_code)}">Copy code</button><button type="button" class="quiet" id="newCode">New code</button><button type="button" class="quiet" id="codeOff">Stop joining</button></div>`
      : `<div><b>Joining is turned off</b><p class="small muted" style="margin:0">Students can't join with a code right now.</p></div><div><button type="button" class="secondary" id="newCode">Turn on joining</button></div>`}</div>
    ${waiting.length ? `<h3 style="margin-top:16px">Waiting for you (${waiting.length})</h3>
      <p class="small muted">Check each student's age group in your school records. Students under 18 can start once you confirm a parent or guardian has agreed.</p>
      ${waiting.map(r => `<div class="waitrow" data-row="${esc(r.id)}"><div><b>${esc(r.name)}</b> <span class="small muted">${esc(r.username || "")}</span>
        ${r.age_band !== "unknown" ? `<div class="small muted">${BAND_TEXT[r.age_band]} · waiting for guardian consent</div>` : ""}</div>
        <div class="approve"><label class="sr" for="b-${esc(r.id)}">Age group</label>${bandSelect(`b-${r.id}`, r.age_band === "unknown" ? "under13" : r.age_band)}
        <label class="chk"><input type="checkbox" id="g-${esc(r.id)}" ${r.guardian_ok ? "checked" : ""}> Guardian agreed</label>
        <button type="button" data-approve="${esc(r.id)}">Approve</button></div></div>`).join("")}` : ""}
    <h3 style="margin-top:16px">Learners</h3>
    ${active.length ? `<div class="scroll"><table><thead><tr><th>Learner</th><th>Age</th><th>Mastered</th><th>Credentials</th><th>Questions</th><th>Last active</th><th></th></tr></thead><tbody>
      ${active.map(r => `<tr><td>${esc(r.name)}${r.username ? `<div class="small muted">${esc(r.username)}</div>` : ""}</td><td class="small">${BAND_TEXT[r.age_band]}</td><td>${r.mastered} / ${size}</td><td>${r.credentials}</td><td>${r.steps}</td><td class="small">${esc(fmtDate(r.last_active))}</td>
        <td><div class="row" style="gap:6px;flex-wrap:nowrap"><button type="button" data-open="${esc(r.id)}">Open</button>${school && r.local ? `<button type="button" class="quiet" data-reset="${esc(r.id)}" data-name="${esc(r.name)}">Reset password</button>` : ""}</div></td></tr>`).join("")}
      </tbody></table></div>` : `<p class="muted small">No learners yet.${c.join_code ? " Share the class code above." : ""}</p>`}
    <div id="classMsg"></div>
    ${school ? `<details style="margin-top:16px"><summary><b>+ Add a student yourself</b> <span class="small muted">· for students who can't use a code</span></summary>
      <form id="fStudent" novalidate><div class="grid2"><div><label class="field" for="sName">First name</label><input type="text" id="sName" maxlength="40"></div>
      <div><label class="field" for="sBand">Age group (from school records)</label>${bandSelect("sBand", "under13")}</div></div>${guardianBox("sGuard")}
      <div class="err" id="sErr" aria-live="assertive"></div><button type="submit">Add student</button></form></details>` : ""}`;
  bindCopy($("cohortBody"));
  const code = async enabled => { try { await api(`/api/educator/cohorts/${encodeURIComponent(c.id)}/code`, { method:"POST", body:{ enabled } }); tabClasses(c.id); } catch (e) { toast(e.message); } };
  on("newCode", "click", () => code(true)); on("codeOff", "click", () => code(false));
  $("cohortBody").querySelectorAll("[data-open]").forEach(b => b.addEventListener("click", () => openLearner(b.dataset.open, c)));
  $("cohortBody").querySelectorAll("[data-approve]").forEach(b => b.addEventListener("click", async () => {
    const id = b.dataset.approve, band = $(`b-${id}`).value, ok = $(`g-${id}`).checked;
    if (band !== "adult" && !ok && !confirm("Without guardian consent this student will keep waiting. Save their age group anyway?")) return;
    b.disabled = true;
    try { await api(`/api/educator/learners/${encodeURIComponent(id)}/approve`, { method:"POST", body:{ age_band:band, guardian_ok:ok } }); toast(band === "adult" || ok ? "Approved. They can start now." : "Saved. They'll wait for guardian consent."); tabClasses(c.id); }
    catch (e) { toast(e.message); b.disabled = false; }
  }));
  $("cohortBody").querySelectorAll("[data-reset]").forEach(b => b.addEventListener("click", async () => {
    if (!confirm(`Reset ${b.dataset.name}'s password? They'll be signed out and choose a new one next time.`)) return;
    try { const r = await api(`/api/educator/learners/${encodeURIComponent(b.dataset.reset)}/password`, { method:"POST" });
      $("classMsg").innerHTML = secretBox(`New sign-in for ${b.dataset.name}`, [["Username", r.username], ["Temporary password", r.temporary_password]], "Give this to the student. It works once: they'll choose their own password straight away."); bindCopy($("classMsg")); $("classMsg").scrollIntoView({ block:"nearest" });
    } catch (e) { toast(e.message); }
  }));
  on("fStudent", "submit", async ev => {
    ev.preventDefault();
    const first_name = $("sName").value.trim();
    if (!first_name) { $("sErr").textContent = "Type the student's first name."; return $("sName").focus(); }
    try { const r = await api(`/api/educator/cohorts/${encodeURIComponent(c.id)}/students`, { method:"POST", body:{ first_name, age_band:$("sBand").value, guardian_ok:$("sGuard").checked } });
      await tabClasses(c.id);
      $("classMsg").innerHTML = secretBox(`Sign-in for ${r.name}`, [["Username", r.username], ["Temporary password", r.temporary_password]], "Give this to the student (or print it). It works once: they'll choose their own password straight away."); bindCopy($("classMsg"));
    } catch (e) { $("sErr").textContent = e.message; }
  });
}

async function openLearner(uid, cls) {
  const track = cls.track || "ml-foundations", q = `?track=${encodeURIComponent(track)}`;
  let m;
  try { m = await api(`/api/educator/learners/${encodeURIComponent(uid)}${q}`); } catch (e) { return toast(e.message); }
  S.me = m;
  const school = S.user.role !== "admin";
  panel(`<button type="button" class="linkish" id="backToCohort">← Back to ${esc(cls.name)}</button>
    <h2 style="margin-top:10px">${esc(m.user.name)}</h2>
    <p class="small muted">${esc(trackName(track))} · ${m.progress.mastered} of ${m.progress.total} skills mastered · ${m.progress.credentials} credentials · age ${esc(BAND_TEXT[m.user.age_band] || "")}</p>
    <div class="layout"><div><h3>Learning path</h3><div id="eduSkills">${pathHtml(m.skills, "Issue credential")}</div></div>
    <div><h3>Credentials</h3><div id="eduCreds">${credsHtml(m.credentials, true)}</div>
    <h3 style="margin-top:16px">Safety guardian</h3><div class="small">${m.interventions.length ? m.interventions.map(i => `<div>Step ${i.step} · ${esc(i.detail)}</div>`).join("") : `<span class="muted">No interventions.</span>`}</div>
    ${school ? `<h3 style="margin-top:16px">Their data</h3><p class="small muted">For a request from the student or their parent or guardian.</p>
      <div class="row"><button type="button" class="secondary" id="lExport">Download their data</button><button type="button" class="quiet" id="lDelete">Delete their account…</button></div><div id="lDelBox"></div>` : ""}</div></div>`);
  on("backToCohort", "click", () => tabClasses(cls.id));
  on("lExport", "click", async () => {
    try { const r = await fetch(`/api/educator/learners/${encodeURIComponent(uid)}/export${q}`, { credentials:"same-origin" }); if (!r.ok) throw new Error();
      const url = URL.createObjectURL(await r.blob()), a = document.createElement("a"); a.href = url; a.download = `cognitive-twin-${m.user.name}.json`; document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
    } catch { toast("Couldn't prepare the download. Please try again."); }
  });
  on("lDelete", "click", () => {
    $("lDelBox").innerHTML = `<div class="banner" style="margin-top:12px"><b>This permanently deletes ${esc(m.user.name)}'s account and learning history.</b>
      <label class="field" for="lDelC">Type <b>delete</b> to confirm</label><input type="text" id="lDelC" autocomplete="off">
      <div class="row"><button type="button" class="danger" id="lDelGo">Delete permanently</button></div></div>`;
    $("lDelC").focus();
    on("lDelGo", "click", async () => {
      if ($("lDelC").value.trim().toLowerCase() !== "delete") return toast('Please type "delete".');
      try { await api(`/api/educator/learners/${encodeURIComponent(uid)}/delete${q}`, { method:"POST", body:{ confirm:"delete" } }); toast("Account deleted."); tabClasses(cls.id); } catch (e) { toast(e.message); }
    });
  });
  $("eduSkills").querySelectorAll("[data-claim]").forEach(b => b.addEventListener("click", async () => {
    b.disabled = true;
    try {
      const r = await api(`/api/educator/learners/${encodeURIComponent(uid)}/credentials${q}`, { method:"POST", body:{ skill:b.dataset.claim } });
      toast(r.issued ? "Credential issued." : `Not issued: ${r.reason}`); openLearner(uid, cls);
    } catch (e) { toast(e.message); b.disabled = false; }
  }));
  bindVerify($("eduCreds"));
  $("eduCreds").querySelectorAll("[data-revoke]").forEach(b => b.addEventListener("click", () => {
    const box = b.closest(".cred").querySelector("[data-result]");
    box.innerHTML = `<div class="inline-form"><label class="sr" for="rr">Reason</label><input type="text" id="rr" placeholder="Reason for revoking (shown to anyone who checks it)">
      <button type="button" class="danger" id="rrGo">Revoke</button></div>`;
    $("rr").focus();
    on("rrGo", "click", async () => {
      const reason = $("rr").value.trim();
      if (reason.length < 5) return toast("Please give a reason of at least 5 characters.");
      try { await api(`/api/educator/credentials/${encodeURIComponent(b.dataset.revoke)}/revoke`, { method:"POST", body:{ reason } }); toast("Credential revoked."); openLearner(uid, cls); }
      catch (e) { toast(e.message); }
    });
  }));
}

async function tabSchool() {
  let sc;
  try { sc = await api("/api/school"); } catch (e) { return panel(`<p class="banner">${esc(e.message)}</p>`); }
  panel(`<div class="stats"><div><b>${sc.learners}</b><span class="small muted">learners</span></div><div><b>${sc.teachers}</b><span class="small muted">teachers</span></div><div><b>${sc.classes}</b><span class="small muted">classes</span></div></div>
    <h2 style="margin-top:18px">Invite staff</h2>
    <p class="small muted">Send a one-time code to a teacher (or another administrator). They choose <b>I'm a teacher with a code</b> on the sign-in page. Codes last 14 days.</p>
    <div class="row"><button type="button" data-invite="educator">Invite a teacher</button><button type="button" class="secondary" data-invite="school_admin">Invite an administrator</button></div>
    <div id="inviteOut"></div>
    <h2 style="margin-top:22px">Staff</h2>
    <div class="scroll"><table><thead><tr><th>Name</th><th>Role</th><th>Username</th><th></th></tr></thead><tbody>
    ${sc.staff.map(u => `<tr><td>${esc(u.name)}</td><td class="small">${u.role === "educator" ? "Teacher" : "Administrator"}</td><td class="small">${esc(u.username || "school sign-in")}</td>
      <td>${u.local && u.id !== S.user.id ? `<button type="button" class="quiet" data-sreset="${esc(u.id)}" data-name="${esc(u.name)}">Reset password</button>` : ""}</td></tr>`).join("")}</tbody></table></div>
    <h2 style="margin-top:22px">Settings</h2>
    <form id="fSchool" novalidate>
      <label class="field" for="yBand">Youngest learners you enrol</label>
      <select id="yBand">${[["under13", "All ages, including under 13"], ["13to17", "13 and over"], ["adult", "18 and over only"]].map(([v, t]) => `<option value="${v}" ${v === sc.youngest_band ? "selected" : ""}>${t}</option>`).join("")}</select>
      <p class="small muted">Learners younger than this can't start, whatever their teacher sets.</p>
      <fieldset class="subjects-pick"><legend><b>Subjects your school offers</b></legend>
        ${sc.all_tracks.map(t => `<label class="chk"><input type="checkbox" name="trk" value="${esc(t.id)}" ${sc.allowed_tracks.includes(t.id) ? "checked" : ""}> ${esc(t.name)} <span class="small muted">${esc(t.audience)}</span></label>`).join("")}</fieldset>
      <div class="err" id="scErr" aria-live="assertive"></div><button type="submit">Save settings</button></form>`);
  document.querySelectorAll("[data-invite]").forEach(b => b.addEventListener("click", async () => {
    try { const r = await api("/api/school/invites", { method:"POST", body:{ role:b.dataset.invite } });
      $("inviteOut").innerHTML = secretBox(r.role === "educator" ? "Teacher invitation" : "Administrator invitation", [["Code", r.code]], "Works once, for 14 days. Send it privately."); bindCopy($("inviteOut"));
    } catch (e) { toast(e.message); }
  }));
  document.querySelectorAll("[data-sreset]").forEach(b => b.addEventListener("click", async () => {
    if (!confirm(`Reset ${b.dataset.name}'s password?`)) return;
    try { const r = await api(`/api/school/staff/${encodeURIComponent(b.dataset.sreset)}/password`, { method:"POST" });
      $("inviteOut").innerHTML = secretBox(`New sign-in for ${b.dataset.name}`, [["Username", r.username], ["Temporary password", r.temporary_password]], "Works once; they choose a new password straight away."); bindCopy($("inviteOut")); $("inviteOut").scrollIntoView({ block:"nearest" });
    } catch (e) { toast(e.message); }
  }));
  on("fSchool", "submit", async ev => {
    ev.preventDefault();
    const allowed = [...document.querySelectorAll("input[name=trk]:checked")].map(x => x.value);
    if (!allowed.length) { $("scErr").textContent = "Choose at least one subject."; return; }
    try { await api("/api/school/settings", { method:"POST", body:{ youngest_band:$("yBand").value, allowed_tracks:allowed } }); toast("Settings saved."); $("scErr").textContent = ""; }
    catch (e) { $("scErr").textContent = e.message; }
  });
}

async function tabSchools() {
  const list = await api("/api/admin/schools").catch(e => { toast(e.message); return []; });
  panel(`<h2>Schools</h2>
    <details ${list.length ? "" : "open"}><summary><b>+ Add a school</b></summary>
      <form id="fNewSchool" novalidate><div class="grid2"><div><label class="field" for="nsName">School name</label><input type="text" id="nsName" maxlength="120"></div>
      <div><label class="field" for="nsRegion">Data region</label><select id="nsRegion"><option value="IN">India</option><option value="UK">United Kingdom</option><option value="EU">European Union</option><option value="US">United States</option></select></div></div>
      <label class="field" for="nsBand">Youngest learners</label><select id="nsBand"><option value="under13">All ages, including under 13</option><option value="13to17">13 and over</option><option value="adult" selected>18 and over only</option></select>
      <div class="err" id="nsErr" aria-live="assertive"></div><button type="submit">Add school</button></form></details>
    <div id="schoolOut"></div>
    <div class="scroll" style="margin-top:14px"><table><thead><tr><th>School</th><th>Sign-in</th><th>Youngest</th><th>Learners</th><th>Teachers</th><th></th></tr></thead><tbody>
    ${list.map(x => `<tr><td>${esc(x.name)}<div class="small muted">${esc(x.region)}</div></td><td class="small">${x.sign_in === "sso" ? "School sign-in" : "Usernames & codes"}</td><td class="small">${BAND_TEXT[x.youngest_band]}</td><td>${x.learners}</td><td>${x.teachers}</td>
      <td><button type="button" class="quiet" data-ainv="${esc(x.id)}" data-name="${esc(x.name)}">Invite administrator</button></td></tr>`).join("") || `<tr><td colspan="6" class="muted">No schools yet.</td></tr>`}</tbody></table></div>`);
  on("fNewSchool", "submit", async ev => {
    ev.preventDefault();
    const name = $("nsName").value.trim();
    if (name.length < 2) { $("nsErr").textContent = "Type the school's name."; return $("nsName").focus(); }
    try { const r = await api("/api/admin/schools", { method:"POST", body:{ name, region:$("nsRegion").value, youngest_band:$("nsBand").value } });
      await tabSchools();
      $("schoolOut").innerHTML = secretBox(`${r.name} is ready`, [["Administrator invitation", r.admin_invite]], "Send this to the school's administrator. They choose <b>I'm a teacher with a code</b>, set a password, then invite their teachers."); bindCopy($("schoolOut"));
    } catch (e) { $("nsErr").textContent = e.message; }
  });
  document.querySelectorAll("[data-ainv]").forEach(b => b.addEventListener("click", async () => {
    try { const r = await api(`/api/admin/schools/${encodeURIComponent(b.dataset.ainv)}/invites`, { method:"POST", body:{ role:"school_admin" } });
      $("schoolOut").innerHTML = secretBox(`Administrator invitation for ${b.dataset.name}`, [["Code", r.code]], "Works once, for 14 days."); bindCopy($("schoolOut"));
    } catch (e) { toast(e.message); }
  }));
}

async function tabPolicies() {
  const ps = await api("/api/admin/policies");
  panel(`<h2>Teaching policies</h2><p class="small muted">Every decision records the policy version that made it. To roll back a bad release, activate an earlier version for that subject; all servers pick it up within about 15 seconds.</p>
    <div class="scroll"><table><thead><tr><th>Subject</th><th>Version</th><th>Notes</th><th>Created</th><th></th></tr></thead><tbody>
    ${ps.map(p => `<tr><td>${esc(p.track_name)}</td><td><code>${esc(p.version)}</code></td><td>${esc(p.notes)}</td><td>${esc(fmtDate(p.created_at))}</td>
      <td>${p.active ? `<span class="badge ok">Active</span>` : `<button type="button" data-act="${esc(p.version)}">Activate</button>`}</td></tr>`).join("")}
    </tbody></table></div><div id="actBox"></div>`);
  document.querySelectorAll("[data-act]").forEach(b => b.addEventListener("click", () => {
    $("actBox").innerHTML = `<div class="inline-form"><label class="sr" for="actReason">Reason</label><input type="text" id="actReason" placeholder="Why activate ${esc(b.dataset.act)}?"><button type="button" id="actGo">Activate</button></div>`;
    $("actReason").focus();
    on("actGo", "click", async () => {
      try { await api(`/api/admin/policies/${encodeURIComponent(b.dataset.act)}/activate`, { method:"POST", body:{ reason:$("actReason").value.trim() } }); toast("Policy activated."); tabPolicies(); }
      catch (e) { toast(e.message); }
    });
  }));
}

async function tabKeys() {
  const [vals, keys] = await Promise.all([api("/api/admin/validators"), api("/api/admin/keys")]);
  panel(`<h2>Validator signing keys</h2><p class="small muted">Rotating adds a new key version and retires the old one. Retired keys stay published, so earlier credentials keep verifying.</p>
    ${vals.map(v => `<div class="card" style="margin-top:10px"><b>${esc(v.name)}</b> <span class="small muted">stake ${v.stake}</span>
      <div class="small">${keys.filter(k => k.validator === v.id).map(k => `<div>v${k.version} · <code>${esc(k.kid)}</code> · <span class="badge ${k.status === "active" ? "ok" : ""}">${esc(k.status)}</span></div>`).join("")}</div>
      <div class="inline-form"><label class="sr" for="arn-${esc(v.id)}">KMS key ARN</label><input type="text" id="arn-${esc(v.id)}" placeholder="KMS key ARN (leave empty on development servers)">
      <button type="button" data-rotate="${esc(v.id)}">Rotate key</button></div></div>`).join("")}`);
  document.querySelectorAll("[data-rotate]").forEach(b => b.addEventListener("click", async () => {
    const arn = document.getElementById(`arn-${b.dataset.rotate}`).value.trim();
    try { const r = await api("/api/admin/keys/rotate", { method:"POST", body:{ validator_id:b.dataset.rotate, kms_key_arn: arn || null } }); toast(`Now on key version ${r.version}.`); tabKeys(); }
    catch (e) { toast(e.message); }
  }));
}

async function tabLedger() {
  panel(`<h2>Credential ledger</h2><p class="small muted">The full hash-chain check also runs at every startup and nightly.</p><button type="button" id="chainBtn">Run full chain check</button><div id="chainOut" role="status" style="margin-top:12px"></div>`);
  on("chainBtn", "click", async () => {
    $("chainOut").textContent = "Checking every block…";
    try { const r = await api("/api/admin/ledger/verify");
      $("chainOut").innerHTML = r.chain_intact ? `<span class="badge ok">Intact</span> ${r.blocks} blocks checked.` : `<span class="badge no">BROKEN</span> First bad block: ${esc(r.first_bad_block)}. Follow the incident runbook.`;
    } catch (e) { $("chainOut").textContent = e.message; }
  });
}

async function tabAudit() {
  const rows = await api("/api/admin/audit?limit=200");
  panel(`<h2>Audit log</h2><p class="small muted">Append-only. Records user IDs, never names.</p><div class="scroll"><table><thead><tr><th>When</th><th>Who</th><th>Action</th><th>Target</th><th>Outcome</th></tr></thead><tbody>
    ${rows.map(r => `<tr><td>${esc(fmtDate(r.at))}</td><td>${esc(r.role || "system")} <span class="small muted">${esc((r.actor || "").slice(0, 8))}</span></td><td>${esc(r.action)}</td><td class="small">${esc(r.target || "")}</td><td>${esc(r.outcome)}</td></tr>`).join("")}
    </tbody></table></div>`);
}

/* ================= demo (simulated student, nothing stored) ================= */
function renderDemoSetup() {
  view(`<section class="card signin"><h1>Watch the tutors teach</h1>
    <p class="muted">A simulated student with their own strengths, learning style and stamina. Nothing is stored.</p>
    <label class="field" for="demoName">Name your demo student</label><input type="text" id="demoName" maxlength="40" value="Ravi">
    <label class="field" for="demoTrack">Subject</label><select id="demoTrack"></select>
    <div class="switch"><input type="checkbox" id="demoBand" checked><label for="demoBand"><b>Give them a focus headband</b> <span class="small muted">(simulated)</span></label></div>
    <div class="row"><button type="button" id="demoGo">Start the demo</button><button type="button" class="quiet" id="demoBack">Back</button></div></section>`);
  on("demoBack", "click", boot);
  catalog().then(list => { $("demoTrack").innerHTML = list.map(t => `<option value="${esc(t.id)}">${esc(t.name)}</option>`).join(""); });
  on("demoGo", "click", async () => {
    $("demoGo").disabled = true; $("demoGo").textContent = "Teaching…";
    try {
      S.demo = await api("/api/demo/run", { method:"POST", body:{ steps:160, track:$("demoTrack").value || "ml-foundations", headband:$("demoBand").checked, name:$("demoName").value.trim() || "Demo student" } });
      S.frame = 0; renderDemo();
    } catch (e) { toast(e.message); $("demoGo").disabled = false; $("demoGo").textContent = "Start the demo"; }
  });
}
function renderDemo() {
  const name = esc(S.demo.name);
  view(`<div class="progress-top"><div style="flex:1"><h1 style="font-size:1.4rem;margin:0">Demo: ${name}</h1><div class="small muted" id="demoProgress"></div>
      <div class="meter" style="margin-top:6px"><i id="demoFill" style="width:0"></i></div></div><button type="button" class="quiet" id="demoExit">Exit demo</button></div>
    <div class="layout"><section class="card" id="stage" aria-live="polite"></section>
      <aside><section class="card"><h2>${name}'s learning path</h2><div id="skills"></div></section>
      <section class="card" style="margin-top:16px"><h2>How ${name} is doing</h2><div id="state"></div></section></aside></div>`);
  on("demoExit", "click", boot);
  drawDemo();
}
function drawDemo() {
  const d = S.demo, f = d.frames[Math.max(0, S.frame - 1)], name = esc(d.name), done = S.frame >= d.frames.length;
  let body = `<p class="muted">Press <b>Play</b> to watch.</p>`;
  if (S.frame > 0) {
    body = f.action === "break" ? `<p class="prompt">☕ ${esc(f.explanation.headline)}</p><p class="muted">${esc((f.explanation.reasons || []).join(" "))}</p>`
      : `<div class="chips"><span class="chip brand">${esc(f.skills.find(s => s.id === f.skill)?.name)}</span><span class="chip">${esc(f.explanation.difficulty_label)}</span><span class="chip">${esc(f.explanation.modality_label)}</span></div>
        <p class="prompt">${f.correct ? "✅" : "❌"} ${name} ${f.correct ? "got it right" : "missed this one"}</p>
        <p class="muted">The tutors are now ${Math.round(f.belief * 100)}% sure.</p>${whyBlock(f.explanation)}`;
  }
  const recent = d.frames.slice(Math.max(0, S.frame - 40), S.frame).map(x => x.action === "break" ? "b" : x.correct ? "c" : "x");
  $("stage").innerHTML = `<h2>Watching ${name} learn</h2>${body}
    <div class="row"><button type="button" id="playBtn" ${done ? "disabled" : ""}>${done ? "Finished" : S.playing ? "⏸ Pause" : "▶ Play"}</button>
      <button type="button" class="secondary" id="stepBtn" ${S.playing || done ? "disabled" : ""}>One step</button>
      <label class="small" for="speed">Speed</label><select id="speed" style="width:auto;min-height:44px"><option value="1500">Slow</option><option value="700">Normal</option><option value="200">Fast</option></select></div>
    <div class="timeline" aria-label="Recent steps">${recent.map(r => `<i class="${r}"></i>`).join("")}</div>
    <div class="legend"><span><i style="background:var(--good)"></i>Right</span><span><i style="background:var(--bad)"></i>Wrong</span><span><i style="background:var(--warn)"></i>Break</span></div>
    <p class="small muted" style="margin-top:8px">Misses are expected: the tutors pick questions slightly above the student's level, because that's where people learn fastest.</p>`;
  $("speed").value = String(S.speed);
  on("speed", "change", e => { S.speed = +e.target.value; if (S.playing) { clearInterval(S.timer); S.timer = setInterval(demoTick, S.speed); } });
  on("playBtn", "click", () => { if (S.playing) { clearInterval(S.timer); S.playing = false; } else { S.playing = true; S.timer = setInterval(demoTick, S.speed); } drawDemo(); $("playBtn").focus({ preventScroll:true }); });
  on("stepBtn", "click", demoTick);
  const skills = (f || d.frames[0]).skills;
  if (S.frame === 0) skills.forEach(s => { s.status = s.status === "learning" ? "ready" : s.status; });
  const mastered = S.frame ? skills.filter(s => s.status === "mastered").length : 0;
  $("demoProgress").textContent = `Step ${S.frame} of ${d.frames.length} · ${mastered} of ${skills.length} skills mastered · policy ${d.policy_version}`;
  $("demoFill").style.width = `${100 * mastered / skills.length}%`;
  $("skills").innerHTML = pathHtml(skills, null);
  const st = f ? f.state : null, word = (x, lo, hi, a, b, c) => x < lo ? a : x < hi ? b : c;
  $("state").innerHTML = d.headband && st ? `<div class="gauges">
      <div class="gauge"><span class="small muted">Focus</span><b>${word(st.attention, .35, .6, "Low", "OK", "Sharp")}</b></div>
      <div class="gauge"><span class="small muted">Effort</span><b>${word(st.load, .4, .7, "Easy", "Just right", "Heavy")}</b></div>
      <div class="gauge"><span class="small muted">Energy</span><b>${word(1 - st.fatigue_estimate, .35, .7, "Tired", "OK", "Fresh")}</b></div></div>
      <p class="small muted" style="margin-top:8px">From the simulated headband. It shapes teaching, never credentials.</p>`
    : `<p class="small">${d.headband ? "Press Play to start." : "No headband: the tutors adapt from answers alone."}</p>`;
}
function demoTick() {
  if (S.frame >= S.demo.frames.length) { clearInterval(S.timer); S.playing = false; return drawDemo(); }
  S.frame += 1;
  if (S.frame >= S.demo.frames.length) { clearInterval(S.timer); S.playing = false; toast("Demo finished."); }
  drawDemo();
}

/* ================= global ================= */
document.addEventListener("keydown", e => {
  if (!S.decision || S.user?.role !== "learner" || e.target.matches("input, select, textarea")) return;
  const k = e.key.toUpperCase(), idx = "ABCD".includes(k) ? "ABCD".indexOf(k) : "1234".indexOf(k);
  if (idx >= 0 && k.length === 1 && !S.answered && S.decision.action === "teach" && document.querySelector(".opt")) { e.preventDefault(); answer(idx); }
  else if (e.key === "Enter" && S.answered && $("nextBtn") && document.activeElement !== $("nextBtn")) { e.preventDefault(); nextStep(); }
});
document.addEventListener("DOMContentLoaded", () => {
  on("howBtn", "click", () => $("howDialog").showModal());
  on("howClose", "click", () => $("howDialog").close());
  on("signOutBtn", "click", signOut);
  boot();
});
