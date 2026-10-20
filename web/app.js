/* monitoring dashboard client -- step 2: shell + polling.
 *
 * No framework, no build step. Polls /api/pulse every 10s; when the db/digest
 * mtime signature changes, the active view refetches. Views land in steps 3-6;
 * the registry below is where they plug in.
 */
"use strict";

/* ------------------------------------------------------------- utilities */

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

const esc = v => String(v ?? "").replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const fmtInt = n => Number(n ?? 0).toLocaleString("en-US");

function fmtUTC(t) {
  if (!t) return "—";
  return new Date(t * 1000).toISOString().slice(0, 16).replace("T", " ") + "Z";
}

function fmtAgo(t) {                       // unix seconds -> "3m ago"
  if (!t) return "—";
  const s = Math.max(0, Math.floor(Date.now() / 1000 - t));
  if (s < 5) return "just now";
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

async function api(path) {
  const r = await fetch(path, { cache: "no-store" });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(body.error || `HTTP ${r.status}`),
                                 { status: r.status });
  return body;
}

/* ----------------------------------------------------------------- state */

const POLL_SECONDS = 10;

const state = {
  view: "overview",
  pulse: null,          // last successful /api/pulse
  pulseError: null,
  signature: null,      // what the active view was rendered from
  timer: null,
  auto: true,
};

/* ------------------------------------------------------------- the views */

/* Each view: { title, load() -> data|null, render(main, data) }.
   Steps 3-6 fill these in; stubs keep the shell honest meanwhile. */
const VIEWS = {
  overview: {
    step: 3,
    render(main) { stub(main, "Overview",
      "freshness, verdict mix, track record, alerts — fed by <code>/api/overview</code>. " +
      "Live counts from <code>/api/pulse</code> already work in the header above."); },
  },
  feeds: {
    step: 4,
    render(main) { stub(main, "Feeds",
      "per-feed canvas charts: values, hour-of-week MAD band, anomaly dots, coverage strip. " +
      "Fed by <code>/api/feeds</code> + <code>/api/feed/N/series</code>."); },
  },
  anomalies: {
    step: 5,
    render(main) { stub(main, "Anomalies",
      "sortable, filterable anomaly table with triage verdicts, linked into the ledger. " +
      "Fed by <code>/api/anomalies</code>."); },
  },
  ledger: {
    step: 5,
    render(main) { stub(main, "Ledger",
      "the prediction ledger: claims, probabilities, resolve windows, HIT/MISS chips, " +
      "independent-origin counts. Fed by <code>/api/predictions</code>."); },
  },
  events: {
    step: 5,
    render(main) { stub(main, "Events",
      "corroboration timeline around a chosen anomaly, origins grouped. " +
      "Fed by <code>/api/events</code>."); },
  },
  digest: {
    step: 6,
    render(main) { stub(main, "Digest",
      "the published digest.md, rendered. Fed by <code>/api/digest</code>."); },
  },
};

function stub(main, title, body) {
  main.innerHTML =
    `<div class="card stub">
       <h2>${esc(title)}</h2>
       <p>${body}</p>
       <p class="dim"><span class="step">step ${VIEWS[state.view].step}</span> brings this view</p>
     </div>`;
}

function setView(name) {
  if (!VIEWS[name]) name = "overview";
  state.view = name;
  $$("#tabs a").forEach(a =>
    a.setAttribute("aria-current", a.dataset.view === name ? "page" : "false"));
  const main = $("#main");
  VIEWS[name].render(main);
}

/* ------------------------------------------------------- header / status */

function renderStatus() {
  const el = $("#status");
  const p = state.pulse, err = state.pulseError;

  if (location.protocol === "file:") {
    el.innerHTML = `<span class="dot warn"></span> open via
      <code>python3 web/serve.py</code> — fetch() is blocked on file://`;
    return;
  }
  if (err) {
    el.innerHTML =
      `<span class="dot err"></span> <span>${esc(err.message)}</span>
       <button id="btn-refresh">retry</button>
       <label><input type="checkbox" id="chk-auto" ${state.auto ? "checked" : ""}> auto</label>`;
    wire();
    return;
  }
  if (!p) return;

  const c = p.counts || {};
  const digest = p.digest_mtime
    ? `<span title="digest mtime ${fmtUTC(p.digest_mtime)}">digest ✓</span>`
    : `<span class="dim">no digest</span>`;
  el.innerHTML =
    `<span class="dot ok"></span> live
     <span class="sep">|</span> <span>obs <span class="num">${fmtInt(c.observations)}</span></span>
     <span>anom <span class="num">${fmtInt(c.anomalies)}</span></span>
     <span>preds <span class="num">${fmtInt(c.predictions)}</span></span>
     <span class="sep">|</span> ${digest}
     <span class="sep">|</span> <span>db touched ${esc(fmtAgo(p.db_mtime))}</span>
     <button id="btn-refresh" title="refetch now">refresh</button>
     <label><input type="checkbox" id="chk-auto" ${state.auto ? "checked" : ""}> auto</label>`;
  wire();
}

function wire() {
  $("#btn-refresh")?.addEventListener("click", () => tick(true));
  $("#chk-auto")?.addEventListener("change", e => {
    state.auto = e.target.checked;
    schedule();
  });
}

/* ------------------------------------------------------------ pulse loop */

function signature(p) {
  return JSON.stringify([p?.db_mtime, p?.digest_mtime, p?.counts]);
}

async function tick(force = false) {
  try {
    const p = await api("/api/pulse");
    state.pulse = p;
    state.pulseError = null;
    const sig = signature(p);
    if (force || sig !== state.signature) {
      state.signature = sig;
      setView(state.view);          // views refetch their own data
    }
  } catch (e) {
    state.pulseError = e;
  }
  renderStatus();
}

function schedule() {
  if (state.timer) clearInterval(state.timer);
  if (state.auto) state.timer = setInterval(tick, POLL_SECONDS * 1000);
}

/* ------------------------------------------------------------------ boot */

window.addEventListener("hashchange", () =>
  setView(location.hash.replace(/^#\//, "") || "overview"));
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) tick();     // catch up after the tab slept
});
window.addEventListener("online", () => tick());

setView(location.hash.replace(/^#\//, "") || "overview");
renderStatus();
tick(true);
schedule();
