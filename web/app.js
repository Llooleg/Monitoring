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

const pct = (x, dec = 0) =>
  x == null ? "—" : `${(100 * x).toFixed(dec)}%`;

const signFmt = (x, dec = 3) =>
  x == null ? "—" : `${x >= 0 ? "+" : ""}${x.toFixed(dec)}`;

const loadingCard = () =>
  `<div class="card"><p class="muted">loading&hellip;</p></div>`;

const errorCard = (msg) =>
  `<div class="banner err">couldn't load this view: ${esc(msg)}</div>`;

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
  seq: 0,               // view-render sequence: stale async renders die silently
};

/* ------------------------------------------------------------- the views */

/* Each view: { render(main, showLoading, seq) }. Steps 4-6 fill the rest in;
   stubs keep the shell honest meanwhile. seq guards against a slow fetch
   resolving after the user switched tabs (stale DOM overwrite). */
const VIEWS = {
  overview: {
    async render(main, showLoading, seq) {
      if (showLoading) main.innerHTML = loadingCard();
      let d;
      try {
        d = await api("/api/overview");
      } catch (e) {
        if (seq === state.seq) main.innerHTML = errorCard(e.message);
        return;
      }
      if (seq !== state.seq) return;
      main.innerHTML = overviewHTML(d);
    },
  },
  feeds: {
    async render(main, showLoading, seq) {
      if (showLoading) main.innerHTML = loadingCard();
      let fl;
      try {
        fl = await api("/api/feeds");
      } catch (e) {
        if (seq === state.seq) main.innerHTML = errorCard(e.message);
        return;
      }
      if (seq !== state.seq) return;
      const feeds = fl.feeds || [];
      const range = state.feedsRange || "14d";
      main.innerHTML = feedsViewHTML(feeds, range);
      $$(".rangebar button", main).forEach(b =>
        b.addEventListener("click", () => {
          state.feedsRange = b.dataset.range;
          setView("feeds", true);
        }));
      for (const f of feeds) {
        const el = $(`#feed-${f.feed_id} .chart-body`, main);
        if (!el) continue;
        if (!f.n_obs) { el.innerHTML = `<p class="dim">no observations yet</p>`; continue; }
        try {
          const s = await api(`/api/feed/${f.feed_id}/series?range=${range}`);
          if (seq !== state.seq) return;
          new FeedChart(el, s);
        } catch (e) {
          el.innerHTML = `<p class="dim">series unavailable: ${esc(e.message)}</p>`;
        }
      }
    },
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

/* ------------------------------------------------------- overview (step 3) */

function kpiCard(label, big, sub) {
  return `<div class="card stat">
    <h2>${esc(label)}</h2>
    <div class="big">${big}</div>
    <div class="sub muted">${sub}</div>
  </div>`;
}

function verdictBlock(v) {
  const total = Object.values(v).reduce((a, b) => a + b, 0);
  if (!total) return `<p class="dim">nothing triaged yet</p>`;
  const kinds = ["call", "noise", "unexplained", "invalid"].filter(k => v[k]);
  const bar = kinds.map(k =>
    `<span class="seg ${k}" style="width:${(100 * v[k] / total).toFixed(1)}%" title="${k} ${fmtInt(v[k])}"></span>`).join("");
  const legend = kinds.map(k =>
    `<div class="lrow"><span class="sw ${k}"></span>${k}
     <b class="num">${fmtInt(v[k])}</b><span class="dim">${pct(v[k] / total)}</span></div>`).join("");
  return `<div class="vbar" title="${fmtInt(total)} triaged">${bar}</div>
          <div class="legend">${legend}</div>`;
}

function freshnessTable(feeds) {
  if (!feeds.length) return `<p class="dim">no feeds in the store yet</p>`;
  const rows = feeds.map(f => {
    const s = f.stale_h;
    const chip = s == null ? `<span class="chip shadow">no data</span>`
      : s <= 2 ? `<span class="chip active">fresh</span>`
      : s <= 48 ? `<span class="chip unexplained">${s}h stale</span>`
      : `<span class="chip invalid">${s}h stale</span>`;
    return `<tr><td>${esc(f.label)}</td>
      <td class="num dim">${esc(f.signal_class)}</td>
      <td class="num">${fmtUTC(f.last_obs)}<span class="dim"> · ${fmtAgo(f.last_obs)}</span></td>
      <td class="num">${fmtInt(f.n_obs)}</td>
      <td>${chip}</td></tr>`;
  }).join("");
  return `<table class="table"><thead><tr>
    <th>feed</th><th>class</th><th>last observation</th><th>n obs</th><th>state</th>
    </tr></thead><tbody>${rows}</tbody></table>`;
}

function trackTable(track) {
  if (!track.length)
    return `<p class="dim">the ledger is empty — no predictions have been made</p>`;
  const rows = track.map(t => {
    const status = t.status
      ? `<span class="chip ${t.status}">${esc(t.status)}</span>`
      : `<span class="dim">unscored</span>`;
    const skillCls = t.skill == null ? "" : t.skill > 0 ? "pos" : t.skill < 0 ? "neg" : "";
    // d = model brier - reference brier: NEGATIVE is better (beats base rate);
    // a class earns alerts when the whole 90% CI sits below zero
    const dCls = t.ci_lo == null ? "" : t.ci_hi < 0 ? "pos" : t.ci_lo > 0 ? "neg" : "";
    const ci = t.ci_lo == null ? "—"
      : `[${signFmt(t.ci_lo, 2)}, ${signFmt(t.ci_hi, 2)}]`;
    return `<tr>
      <td class="num">${esc(t.signal_class)}</td>
      <td>${status}</td>
      <td class="num">${fmtInt(t.n_total)}</td>
      <td class="num">${fmtInt(t.n_resolved)}</td>
      <td class="num">${pct(t.hit_rate)}</td>
      <td class="num">${t.brier == null ? "—" : t.brier.toFixed(3)}</td>
      <td class="num dim">${t.brier_ref == null ? "—" : t.brier_ref.toFixed(3)}</td>
      <td class="num ${skillCls}">${t.skill == null ? "—" : signFmt(t.skill, 2)}</td>
      <td class="num ${dCls}">${t.d_mean == null ? "—" : signFmt(t.d_mean, 2)} <span class="dim">${ci}</span></td>
    </tr>`;
  }).join("");
  return `<table class="table"><thead><tr>
    <th>class</th><th>status</th><th>n</th><th>resolved</th><th>hit</th>
    <th>brier</th><th>ref</th><th>skill</th><th>&Delta; vs base [90% CI]</th>
    </tr></thead><tbody>${rows}</tbody></table>
    <p class="dim">&Delta; = model Brier &minus; base-rate Brier: negative means the class beats the
    base rate. Classes start <b>shadow</b> (logged, scored, never shown); they earn alerts only
    when the full CI drops below zero.</p>`;
}

function alertsBlock(alerts) {
  if (!alerts.length)
    return `<p class="dim">No alerts. A class must beat the base rate with confidence before
            anything is allowed through — silence here is the design, not a bug.</p>`;
  const rows = alerts.map(a => `<tr>
    <td class="num">${fmtUTC(a.sent_at)}</td>
    <td class="num dim">${esc(a.signal_class)}</td>
    <td>${esc(a.claim)}</td></tr>`).join("");
  return `<table class="table"><thead><tr>
    <th>sent</th><th>class</th><th>claim</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function overviewHTML(d) {
  const p = d.predictions || {};
  const resolved = (p.yes || 0) + (p.no || 0);
  const hitRate = resolved ? p.yes / resolved : null;
  const an = d.anomalies || {};
  const dig = d.digest || {};

  const kpis = [
    kpiCard("data as of", `<span class="num">${fmtUTC(d.as_of)}</span>`, `${fmtInt((d.feeds || []).length)} feeds in store`),
    kpiCard("anomalies", fmtInt(an.h24), `${fmtInt(an.d7)} in 7d &middot; ${fmtInt(an.total)} total`),
    kpiCard("predictions", fmtInt(p.total), hitRate == null ? (p.open ? `${p.open} open` : "none resolved") : `${p.yes} hit &middot; ${p.no} miss &middot; ${p.open || 0} open`),
    kpiCard("hit rate", hitRate == null ? "—" : `<span class="num">${pct(hitRate)}</span>`, resolved ? `of ${resolved} resolved by rule` : "nothing resolved yet"),
    kpiCard("alerts", fmtInt((d.alerts || []).length), (d.alerts || []).length ? "see below" : "none earned yet"),
    kpiCard("miscounts", fmtInt(d.miscounts), d.miscounts ? "claimed &gt; computed origins — check prompt" : "agent counts honestly"),
    kpiCard("digest", dig.available ? "✓" : "—", dig.available ? `written ${fmtAgo(dig.mtime)}` : "not found"),
  ];

  return `
  <div class="grid kpis">${kpis.join("")}</div>
  <div class="grid two">
    <div class="card"><h2>Feed freshness</h2>${freshnessTable(d.feeds || [])}</div>
    <div class="card"><h2>Verdict mix <span class="dim">(all time)</span></h2>${verdictBlock(d.verdicts || {})}</div>
  </div>
  <div class="card"><h2>Track record — Brier skill vs base rate</h2>${trackTable(d.track_record || [])}</div>
  <div class="card"><h2>Alerts</h2>${alertsBlock(d.alerts || [])}</div>`;
}

function setView(name, showLoading = true) {
  if (!VIEWS[name]) name = "overview";
  state.view = name;
  const seq = ++state.seq;
  $$("#tabs a").forEach(a =>
    a.setAttribute("aria-current", a.dataset.view === name ? "page" : "false"));
  VIEWS[name].render($("#main"), showLoading, seq);
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
      setView(state.view, false);     // background refresh: no loading flicker
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

if (typeof document !== "undefined") {
  window.addEventListener("hashchange", () =>
    setView(location.hash.replace(/^#\//, "") || "overview"));
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) tick();   // catch up after the tab slept
  });
  window.addEventListener("online", () => tick());

  setView(location.hash.replace(/^#\//, "") || "overview");
  renderStatus();
  tick(true);
  schedule();
}

/* ---------------------------------------------------------- feeds (step 4) */

function feedsViewHTML(feeds, range) {
  const btn = (r, label) =>
    `<button data-range="${r}" class="${r === range ? "on" : ""}">${label}</button>`;
  return `
  <div class="card rangecard">
    <h2>Feeds</h2>
    <div class="rangebar">${btn("3d", "3 days")}${btn("14d", "14 days")}${btn("12w", "12 weeks")}</div>
    <span class="dim">values at first-reported vintage · band = hour-of-week median ±3·MAD (trailing 12w) · shaded columns = weekend</span>
  </div>
  ${feeds.map(f => `
    <div class="card feedcard" id="feed-${f.feed_id}">
      <div class="fhead">
        <div><span class="flabel">${esc(f.label)}</span>
          <span class="chip shadow num">${esc(f.signal_class)}</span>
          ${f.n_untriaged ? `<span class="chip unexplained">${f.n_untriaged} untriaged</span>` : ""}
        </div>
        <div class="dim num">${fmtInt(f.n_obs)} obs · fired ${f.fired_365d ?? "—"}/365d · ${esc(f.source || "")}</div>
      </div>
      <div class="chart-body"></div>
    </div>`).join("")}`;
}

/* node smoke hook: render views without a DOM (see tests/ and README) */
if (typeof module !== "undefined" && module.exports) {
  module.exports = { overviewHTML, verdictBlock, freshnessTable, trackTable, alertsBlock,
                     feedsViewHTML };
}
