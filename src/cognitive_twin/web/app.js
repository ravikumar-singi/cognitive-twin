"use strict";
/* Cognitive Twin web app. Served as a separate file so the Content-Security-Policy can
   forbid inline scripts. All server text is escaped before it touches innerHTML. */

const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const LETTERS = ["A", "B", "C", "D"];
const ICON = { locked:"🔒", ready:"○", learning:"…", mastered:"✓", credentialed:"★" };
const STATUS_TEXT = { locked:"Locked", ready:"Ready", learning:"Learning", mastered:"Mastered", credentialed:"Credential earned" };

const S = { csrf:null, user:null, cfg:null, me:null, decision:null, answered:false, busy:false,
            breakTimer:null, demo:null, playing:false, timer:null, frame:0, speed:700 };

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
  S.cfg = S.cfg || await api("/auth/config").catch(() => ({ sso:false, dev_login:false, personas:[], demo:false }));
  try {
    const s = await api("/auth/session");
    S.user = s.user; S.csrf = s.user ? (s.csrf || S.csrf) : null;
  } catch { S.user = null; }
  $("who").hidden = $("roleChip").hidden = $("signOutBtn").hidden = !S.user;
  if (!S.user) return renderSignIn();
  $("who").textContent = S.user.name; $("roleChip").textContent = S.user.role;
  if (S.user.role === "learner") return renderLearner();
  return renderStaff();
}

function renderSignIn() {
  const c = S.cfg;
  view(`<section class="card signin">
    <h1>Welcome to Cognitive Twin</h1>
    <p class="muted">A team of AI tutors that adapts to how you learn, and credentials you can prove.</p>
    ${c.sso ? `<button class="big" id="ssoBtn" type="button">Sign in with your institution</button>` : ""}
    ${!c.sso && !c.dev_login ? `<p class="banner">Sign-in isn't configured on this server yet. Ask your administrator.</p>` : ""}
    ${c.dev_login ? `<h2 style="margin-top:20px">Development sign-in</h2>
      <p class="small muted">Only on development servers. Each person below is what an institution's records would say about them.</p>
      <div class="persona-list">${c.personas.map(p => `<button type="button" data-persona="${esc(p.id)}">${esc(p.name)}</button>`).join("")}</div>` : ""}
    ${c.demo ? `<p style="margin-top:18px"><button class="secondary" type="button" id="demoBtn">Watch a demo first</button></p>` : ""}
  </section>`);
  on("ssoBtn", "click", () => { location.href = "/auth/login"; });
  on("demoBtn", "click", renderDemoSetup);
  document.querySelectorAll("[data-persona]").forEach(b => b.addEventListener("click", async () => {
    b.disabled = true;
    const r = await fetch("/auth/dev-login", { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({ persona: b.dataset.persona }) });
    if (!r.ok) { toast("Sign-in failed."); b.disabled = false; return; }
    S.csrf = r.headers.get("X-CSRF-Token"); boot();
  }));
}

async function signOut() {
  try { await api("/auth/logout", { method:"POST" }); } catch {}
  S.user = null; S.csrf = null; boot();
}

/* ================= learner ================= */
async function renderLearner() {
  try { S.me = await api("/api/me"); } catch (e) { return view(`<p class="banner">${esc(e.message)}</p>`); }
  const c = S.me.consent;
  if (!c.eligible) {
    return view(`<section class="card signin"><h1>This pilot is for adult learners</h1>
      <p>Your institution's records show you're not yet eligible for this pilot, which is open to learners aged 18 and over.</p>
      <p class="muted small">If you think this is wrong, contact your institution's registry office.</p></section>`);
  }
  if (!c.learning_granted) return renderConsent(c);
  renderLearning();
  if (S.me.pending) showDecision(S.me.pending); else nextStep();
}

function renderConsent(c) {
  view(`<section class="card signin" aria-labelledby="ct">
    <h1 id="ct">${c.learning_needs_renewal ? "Our data policy has changed" : "Before you start"}</h1>
    ${c.learning_needs_renewal ? `<p class="banner">Please read the updated policy and agree again to keep learning.</p>` : ""}
    <div class="policy" tabindex="0" aria-label="Data policy">${esc(c.policy_text)}</div>
    <p class="small muted">Policy version ${esc(c.policy_version)}</p>
    <div class="switch"><input type="checkbox" id="agree"><label for="agree"><b>I agree to this data policy</b><br><span class="small muted">Required to use the tutors.</span></label></div>
    <div class="switch"><input type="checkbox" id="neural"><label for="neural"><b>Optional: I'd allow the focus headband</b><br><span class="small muted">${esc(c.neural_policy_text)}</span></label></div>
    <div class="err" id="consentErr" aria-live="assertive"></div>
    <button class="big" type="button" id="consentBtn">Agree and start</button>
  </section>`);
  on("consentBtn", "click", async () => {
    if (!$("agree").checked) { $("consentErr").textContent = "Please tick the box to agree, or sign out if you don't want to continue."; return $("agree").focus(); }
    $("consentBtn").disabled = true;
    try { await api("/api/me/consent", { method:"POST", body:{ learning:true, neural_sensing:$("neural").checked } }); renderLearner(); }
    catch (e) { $("consentErr").textContent = e.message; $("consentBtn").disabled = false; }
  });
}

function renderLearning() {
  view(`<div class="progress-top"><div style="flex:1;min-width:220px">
      <h1 style="font-size:1.4rem;margin:0">Hi ${esc(S.me.user.name.split(" ")[0])} 👋</h1>
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
          <p class="small muted">Download everything we hold about you, or delete it. Credentials you've earned stay verifiable but can no longer be linked to you.</p>
          <div class="row"><button type="button" class="secondary" id="exportBtn">Download my data</button>
          <button type="button" class="quiet" id="deleteBtn">Delete my data…</button></div>
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
  renderSide();
}

async function refreshMe() { S.me = await api("/api/me"); renderSide(); }

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
  try { showDecision(await api("/api/me/next", { method:"POST", body:{ skip_break: skipBreak } })); }
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
    const r = await api("/api/me/answer", { method:"POST", body:{ item_id: S.decision.item.id, choice: i } });
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
    $("stage").innerHTML = `<div class="center"><h2 style="font-size:1.6rem">🎉 You've mastered every skill!</h2><p class="muted">Claim your remaining credentials from your learning path.</p></div>`;
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
    try { await api("/api/me/break", { method:"POST" }); await refreshMe(); nextStep(); } catch (e) { toast(e.message); }
  });
  $("backBtn").focus({ preventScroll:true });
}
async function claim(skill, btn) {
  btn.disabled = true; btn.textContent = "Validators are checking…";
  try {
    const r = await api("/api/me/credentials", { method:"POST", body:{ skill } });
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

/* ================= educators and administrators ================= */
async function renderStaff() {
  const admin = S.user.role === "admin";
  const tabs = [["cohorts", "Cohorts"], ...(admin ? [["policies", "Teaching policies"], ["keys", "Signing keys"], ["ledger", "Ledger"], ["audit", "Audit log"]] : [])];
  view(`<h1 style="font-size:1.4rem">${admin ? "Administration" : "Your cohorts"}</h1>
    <div class="tabs" role="tablist">${tabs.map(([id, label], i) => `<button type="button" role="tab" data-tab="${id}" aria-selected="${i === 0}">${label}</button>`).join("")}</div>
    <section class="card" id="panel"></section>`);
  document.querySelectorAll("[data-tab]").forEach(b => b.addEventListener("click", () => {
    document.querySelectorAll("[data-tab]").forEach(x => x.setAttribute("aria-selected", x === b));
    ({ cohorts: tabCohorts, policies: tabPolicies, keys: tabKeys, ledger: tabLedger, audit: tabAudit })[b.dataset.tab]();
  }));
  tabCohorts();
}
const panel = html => { $("panel").innerHTML = html; };

async function tabCohorts() {
  const cohorts = await api("/api/educator/cohorts").catch(e => { toast(e.message); return []; });
  if (!cohorts.length) return panel(`<p class="muted">No cohorts are assigned to you yet. Cohorts come from your institution's records.</p>`);
  panel(`<label class="field" for="cohortSel">Cohort</label><select id="cohortSel">${cohorts.map(c => `<option value="${esc(c.id)}">${esc(c.name)} (${c.learners} learners)</option>`).join("")}</select><div id="cohortBody" style="margin-top:14px"></div>`);
  on("cohortSel", "change", e => loadCohort(e.target.value));
  loadCohort(cohorts[0].id);
}
async function loadCohort(id) {
  const rows = await api(`/api/educator/cohorts/${encodeURIComponent(id)}/learners`);
  $("cohortBody").innerHTML = rows.length ? `<div class="scroll"><table><thead><tr><th>Learner</th><th>Mastered</th><th>Credentials</th><th>Questions</th><th>Last active</th><th></th></tr></thead><tbody>
    ${rows.map(r => `<tr><td>${esc(r.name)}</td><td>${r.mastered} / 7</td><td>${r.credentials}</td><td>${r.steps}</td><td>${esc(fmtDate(r.last_active))}</td><td><button type="button" data-open="${esc(r.id)}">Open</button></td></tr>`).join("")}
    </tbody></table></div>` : `<p class="muted">No learners in this cohort yet.</p>`;
  $("cohortBody").querySelectorAll("[data-open]").forEach(b => b.addEventListener("click", () => openLearner(b.dataset.open, id)));
}
async function openLearner(uid, cohortId) {
  let m;
  try { m = await api(`/api/educator/learners/${encodeURIComponent(uid)}`); } catch (e) { return toast(e.message); }
  S.me = m;
  panel(`<button type="button" class="linkish" id="backToCohort">← Back to cohort</button>
    <h2 style="margin-top:10px">${esc(m.user.name)}</h2>
    <p class="small muted">${m.progress.mastered} of ${m.progress.total} skills mastered · ${m.progress.credentials} credentials · region ${esc(m.user.region)}</p>
    <div class="layout"><div><h3>Learning path</h3><div id="eduSkills">${pathHtml(m.skills, "Issue credential")}</div></div>
    <div><h3>Credentials</h3><div id="eduCreds">${credsHtml(m.credentials, true)}</div>
    <h3 style="margin-top:16px">Safety guardian</h3><div class="small">${m.interventions.length ? m.interventions.map(i => `<div>Step ${i.step} · ${esc(i.detail)}</div>`).join("") : `<span class="muted">No interventions.</span>`}</div></div></div>`);
  on("backToCohort", "click", tabCohorts);
  $("eduSkills").querySelectorAll("[data-claim]").forEach(b => b.addEventListener("click", async () => {
    b.disabled = true;
    try {
      const r = await api(`/api/educator/learners/${encodeURIComponent(uid)}/credentials`, { method:"POST", body:{ skill:b.dataset.claim } });
      toast(r.issued ? "Credential issued." : `Not issued: ${r.reason}`); openLearner(uid, cohortId);
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
      try { await api(`/api/educator/credentials/${encodeURIComponent(b.dataset.revoke)}/revoke`, { method:"POST", body:{ reason } }); toast("Credential revoked."); openLearner(uid, cohortId); }
      catch (e) { toast(e.message); }
    });
  }));
}

async function tabPolicies() {
  const ps = await api("/api/admin/policies");
  panel(`<h2>Teaching policies</h2><p class="small muted">Every decision records the policy version that made it. To roll back a bad release, activate an earlier version; all servers pick it up within about 15 seconds.</p>
    <div class="scroll"><table><thead><tr><th>Version</th><th>Notes</th><th>Created</th><th></th></tr></thead><tbody>
    ${ps.map(p => `<tr><td><code>${esc(p.version)}</code></td><td>${esc(p.notes)}</td><td>${esc(fmtDate(p.created_at))}</td>
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
    <div class="switch"><input type="checkbox" id="demoBand" checked><label for="demoBand"><b>Give them a focus headband</b> <span class="small muted">(simulated)</span></label></div>
    <div class="row"><button type="button" id="demoGo">Start the demo</button><button type="button" class="quiet" id="demoBack">Back</button></div></section>`);
  on("demoBack", "click", boot);
  on("demoGo", "click", async () => {
    $("demoGo").disabled = true; $("demoGo").textContent = "Teaching…";
    try {
      S.demo = await api("/api/demo/run", { method:"POST", body:{ steps:160, headband:$("demoBand").checked, name:$("demoName").value.trim() || "Demo student" } });
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
