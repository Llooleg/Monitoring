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

/* Each view: { render(main, showLoading, seq) }. seq guards against a slow
   fetch resolving after the user switched tabs (stale DOM overwrite). */
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
    async render(main, showLoading, seq) {
      if (showLoading) main.innerHTML = loadingCard();
      const f = state.anomFilter || (state.anomFilter = { verdict: "", feed_id: "" });
      const sort = state.anomSort || (state.anomSort = { key: "detected_at", dir: -1 });
      let aj, fj;
      try {
        [aj, fj] = await Promise.all([
          api(`/api/anomalies?limit=1000${f.verdict ? `&verdict=${f.verdict}` : ""}${f.feed_id ? `&feed_id=${f.feed_id}` : ""}`),
          api("/api/feeds"),
        ]);
      } catch (e) {
        if (seq === state.seq) main.innerHTML = errorCard(e.message);
        return;
      }
      if (seq !== state.seq) return;
      main.innerHTML = anomaliesHTML(aj, fj.feeds || [], f, sort);
      $("#f-verdict", main).addEventListener("change", e => { f.verdict = e.target.value; setView("anomalies", false); });
      $("#f-feed", main).addEventListener("change", e => { f.feed_id = e.target.value; setView("anomalies", false); });
      $$("th.sortable", main).forEach(th => th.addEventListener("click", () => {
        const k = th.dataset.key;
        if (sort.key === k) sort.dir *= -1; else { sort.key = k; sort.dir = -1; }
        setView("anomalies", false);
      }));
      $$(".plink", main).forEach(a => a.addEventListener("click", () => { state.ledgerFocus = +a.dataset.pid; }));
    },
  },
  ledger: {
    async render(main, showLoading, seq) {
      if (showLoading) main.innerHTML = loadingCard();
      const sort = state.ledgerSort || (state.ledgerSort = { key: "made_at", dir: -1 });
      let pj;
      try {
        pj = await api("/api/predictions");
      } catch (e) {
        if (seq === state.seq) main.innerHTML = errorCard(e.message);
        return;
      }
      if (seq !== state.seq) return;
      main.innerHTML = ledgerHTML(pj.predictions || [], sort, Date.now() / 1000);
      $$("th.sortable", main).forEach(th => th.addEventListener("click", () => {
        const k = th.dataset.key;
        if (sort.key === k) sort.dir *= -1; else { sort.key = k; sort.dir = -1; }
        setView("ledger", false);
      }));
      $$(".exp", main).forEach(b => b.addEventListener("click", () => {
        const row = b.closest("tr"), det = row.nextElementSibling;
        det.hidden = !det.hidden;
        b.textContent = det.hidden ? "+" : "\u2212";
      }));
      if (state.ledgerFocus) {
        const tr = $(`.prow[data-pid="${state.ledgerFocus}"]`, main);
        if (tr) {
          tr.classList.add("hl");
          const det = tr.nextElementSibling;
          det.hidden = false;
          tr.querySelector(".exp").textContent = "\u2212";
          tr.scrollIntoView({ block: "center" });
        }
        state.ledgerFocus = null;
      }
    },
  },
  events: {
    async render(main, showLoading, seq) {
      if (showLoading) main.innerHTML = loadingCard();
      const range = state.eventsRange || (state.eventsRange = "14d");
      const type = state.eventsType ?? (state.eventsType = "");
      let ej;
      try {
        // anchor to the newest event in the store (replays), then window back
        const until = state.eventsUntil || "";
        const q = until ? `?since=${Math.floor(until - EVENT_RANGES[range] * 3600)}&until=${until}` : "";
        ej = await api(`/api/events${q}`);
        state.eventsUntil = ej.until;
      } catch (e) {
        if (seq === state.seq) main.innerHTML = errorCard(e.message);
        return;
      }
      if (seq !== state.seq) return;
      main.innerHTML = eventsHTML(ej, range, type);
      $$(".rangebar button[data-range]", main).forEach(b =>
        b.addEventListener("click", () => { state.eventsRange = b.dataset.range; setView("events", false); }));
      $$(".typebar button[data-type]", main).forEach(b =>
        b.addEventListener("click", () => { state.eventsType = b.dataset.type; setView("events", false); }));
    },
  },
  digest: {
    async render(main, showLoading, seq) {
      if (showLoading) main.innerHTML = loadingCard();
      let d;
      try {
        d = await api("/api/digest");
      } catch (e) {
        if (seq === state.seq) main.innerHTML = errorCard(e.message);
        return;
      }
      if (seq !== state.seq) return;
      main.innerHTML = digestViewHTML(d);
    },
  },
};

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

/* ------------------------------------------------------ shared helpers */

function sortBy(arr, key, dir) {
  return [...arr].sort((a, b) => {
    const x = a[key], y = b[key];
    if (x == null && y == null) return 0;
    if (x == null) return 1;          // nulls sink regardless of direction
    if (y == null) return -1;
    return (x < y ? -1 : x > y ? 1 : 0) * dir;
  });
}

function thSort(label, key, sort) {
  const on = sort.key === key;
  return `<th class="sortable${on ? " on" : ""}" data-key="${key}">${label}${on ? (sort.dir < 0 ? " &#9662;" : " &#9652;") : ""}</th>`;
}

function zClass(z) {
  const a = Math.abs(z);
  return a >= 8 ? "neg" : a >= 6 ? "warn" : "";
}

function verdictChip(v) {
  return v ? `<span class="chip ${v}">${v}</span>`
           : `<span class="chip shadow">untriaged</span>`;
}

function outcomeChip(p, nowS) {
  if (p.outcome === "yes") return `<span class="chip hit">HIT</span>`;
  if (p.outcome === "no") return `<span class="chip miss">MISS</span>`;
  if (p.outcome === "void") return `<span class="chip shadow">VOID</span>`;
  if (p.resolve_by < nowS) return `<span class="chip unexplained">OVERDUE</span>`;
  return `<span class="chip open">OPEN</span>`;
}

const fmtShort = t => {
  if (!t) return "—";
  const d = new Date(t * 1000);
  const M = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${M[d.getUTCMonth()]} ${d.getUTCDate()} ${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`;
};

/* --------------------------------------------------- anomalies (step 5) */

function anomaliesHTML(aj, feeds, filter, sort) {
  const rows = sortBy(aj.anomalies || [], sort.key, sort.dir).map(a => `
    <tr>
      <td class="num">${fmtShort(a.observed_at)}<span class="dim"> · ${fmtAgo(a.observed_at)}</span></td>
      <td>${esc(a.label)}</td>
      <td class="num dim">${esc(a.signal_class)}</td>
      <td class="num ${zClass(a.z)}">${a.z.toFixed(1)}</td>
      <td>${verdictChip(a.verdict)}</td>
      <td class="sum" title="${esc(a.summary || "")}">${esc(a.summary || "")}</td>
      <td>${a.pred_id ? `<a href="#/ledger" class="plink" data-pid="${a.pred_id}">#${a.pred_id}</a>` : "<span class=\"dim\">—</span>"}</td>
    </tr>`).join("");
  const opt = (v, l, cur) => `<option value="${v}"${v === cur ? " selected" : ""}>${l}</option>`;
  return `
  <div class="card">
    <h2>Anomalies <span class="dim num">${fmtInt(aj.total)} total</span></h2>
    <div class="filterbar">
      <select id="f-verdict">${opt("", "all verdicts", filter.verdict)}${["call", "noise", "unexplained", "invalid"].map(v => opt(v, v, filter.verdict)).join("")}</select>
      <select id="f-feed">${opt("", "all feeds", filter.feed_id)}${feeds.map(f => opt(String(f.feed_id), f.label, filter.feed_id)).join("")}</select>
      ${aj.total > (aj.anomalies || []).length ? `<span class="dim">showing first ${fmtInt(aj.anomalies.length)} by detection time</span>` : ""}
    </div>
    <table class="table"><thead><tr>
      ${thSort("observed", "observed_at", sort)}
      <th>feed</th><th>class</th>
      ${thSort("z", "z", sort)}
      ${thSort("verdict", "verdict", sort)}
      <th>triage summary</th><th>ledger</th>
    </tr></thead><tbody>${rows || `<tr><td colspan="7" class="dim">nothing matches</td></tr>`}</tbody></table>
  </div>`;
}

/* ------------------------------------------------------- ledger (step 5) */

function ledgerHTML(preds, sort, nowS) {
  const rows = sortBy(preds, sort.key, sort.dir).map(p => {
    const winH = Math.round((p.resolve_by - p.made_at) / 3600);
    return `
    <tr class="prow" data-pid="${p.pred_id}">
      <td class="num">#${p.pred_id}</td>
      <td class="num">${fmtShort(p.made_at)}</td>
      <td class="num dim">${esc(p.signal_class)}</td>
      <td class="claim" title="${esc(p.claim)}">${esc(p.claim)}</td>
      <td class="num">${pct(p.probability)}</td>
      <td class="num dim">${winH}h</td>
      <td class="num">${p.n_independent ?? "—"}</td>
      <td>${outcomeChip(p, nowS)}</td>
      <td>${p.alerted ? `<span title="alerted">\u{1F514}</span>` : ""}</td>
      <td><button class="exp" aria-label="expand">+</button></td>
    </tr>
    <tr class="pdetail" hidden><td colspan="10">
      <dl class="kv">
        <dt>claim</dt><dd>${esc(p.claim)}</dd>
        <dt>falsifier</dt><dd>${esc(p.falsifier)}</dd>
        <dt>resolution rule</dt><dd><pre>${esc(JSON.stringify(p.resolution_rule, null, 1))}</pre></dd>
        <dt>resolution</dt><dd>${p.outcome ? `${esc(p.outcome)} by ${esc(p.resolver || "?")} at ${fmtUTC(p.resolved_at)}${p.resolution_note ? ` — ${esc(p.resolution_note)}` : ""}` : `due ${fmtUTC(p.resolve_by)}`}</dd>
        <dt>anomaly</dt><dd>${esc(p.anomaly_feed || "?")} · ${fmtUTC(p.anomaly_at)} · z=${p.z == null ? "—" : p.z.toFixed(1)}</dd>
        <dt>model</dt><dd>${esc(p.model)} · prompt ${esc(p.prompt_version)}</dd>
        <dt>independent origins</dt><dd>${p.n_independent ?? "—"} (recomputed by v_independent_support, not the model's claim)</dd>
      </dl>
    </td></tr>`;
  }).join("");
  return `
  <div class="card">
    <h2>Prediction ledger <span class="dim">immutable, one per anomaly, resolved by rule — never by the model</span></h2>
    <table class="table"><thead><tr>
      ${thSort("id", "pred_id", sort)}
      ${thSort("made", "made_at", sort)}
      <th>class</th><th>claim</th>
      ${thSort("p", "probability", sort)}
      <th>window</th>
      ${thSort("origins", "n_independent", sort)}
      ${thSort("outcome", "outcome", sort)}
      <th></th><th></th>
    </tr></thead><tbody>${rows || `<tr><td colspan="10" class="dim">the ledger is empty</td></tr>`}</tbody></table>
  </div>`;
}

/* ------------------------------------------------------- events (step 5) */

const EVENT_RANGES = { "24h": 24, "3d": 72, "7d": 168, "14d": 336 };

function eventsHTML(ej, range, typeFilter) {
  const evs = ej.events || [];
  const types = [...new Set(evs.map(e => e.event_type))].sort();
  const shown = typeFilter ? evs.filter(e => e.event_type === typeFilter) : evs;

  // group by origin: N reposts of one thing are ONE line, like the mock judges it
  const groups = new Map();
  for (const e of shown) {
    const k = e.origin_key || `~no-origin/${e.event_id}`;
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(e);
  }
  const ordered = [...groups.entries()].sort((a, b) => b[1][0].occurred_at - a[1][0].occurred_at);

  const rbtn = (r, l) => `<button data-range="${r}" class="${r === range ? "on" : ""}">${l}</button>`;
  const tbtn = t => `<button data-type="${t}" class="${typeFilter === t ? "on" : ""}">${t} (${evs.filter(e => e.event_type === t).length})</button>`;

  const body = ordered.map(([origin, es]) => `
    <div class="ogroup">
      <div class="ohead"><b>${esc(String(origin).slice(0, 48))}</b> · ${es.length} event${es.length > 1 ? "s" : ""}
        <span class="dim">${fmtShort(es[0].occurred_at)} → ${fmtShort(es[es.length - 1].occurred_at)} · 1 independent origin</span></div>
      ${es.map(e => `
        <div class="eitem">
          <span class="et">${fmtShort(e.occurred_at)}</span>
          <span class="tchip ${esc(e.event_type)}">${esc(e.event_type)}</span>
          <span>${esc(e.title || "(untitled)")}${e.magnitude != null ? ` <span class="dim num">mag ${e.magnitude}</span>` : ""}</span>
          <span class="dim">${esc(e.source)}</span>
        </div>`).join("")}
    </div>`).join("");

  return `
  <div class="card">
    <h2>Events <span class="dim num">window ${fmtShort(ej.since)} → ${fmtShort(ej.until)}</span></h2>
    <div class="filterbar">
      <div class="rangebar">${rbtn("24h", "24h")}${rbtn("3d", "3d")}${rbtn("7d", "7d")}${rbtn("14d", "14d")}</div>
      <div class="rangebar typebar"><button data-type="" class="${typeFilter ? "" : "on"}">all</button>${types.map(t => tbtn(t)).join("")}</div>
    </div>
    <p class="dim num">${fmtInt(shown.length)} events · ${fmtInt(ordered.length)} independent origins — repost storms collapse to one line</p>
    ${body || `<p class="dim">no events in this window</p>`}
  </div>`;
}

/* ------------------------------------------------------- digest (step 6) */

/* Mini renderer for digest.py's constrained plain-text format (NOT general
   markdown): title, health line, placebo line, ALLCAPS sections with (n),
   '·' bullets, HIT./MISS. prefixes, fixed-width TRACK RECORD table, footer
   summaries. Everything is escaped per line before any markup is added. */
function renderDigest(text) {
  const out = [];
  let pre = [];
  const flushPre = () => {
    if (pre.length) { out.push(`<pre class="dtable">${pre.map(esc).join("\n")}</pre>`); pre = []; }
  };

  for (const raw of String(text || "").split(/\r?\n/)) {
    const line = raw.replace(/\s+$/, "");
    if (!line.trim()) { flushPre(); continue; }

    if (/^DAILY DIGEST/.test(line)) {
      flushPre(); out.push(`<div class="dtitle">${esc(line)}</div>`); continue;
    }
    if (/^pipeline (ok|degraded)/i.test(line)) {
      flushPre();
      out.push(`<div class="banner ${/degraded/i.test(line) ? "err" : "ok"}">${esc(line)}</div>`);
      continue;
    }
    if (/^Placebo check:/.test(line)) {
      flushPre();
      out.push(`<div class="dplacebo${line.includes("!!") ? " warn-line" : ""}">${esc(line)}</div>`);
      continue;
    }
    // footer summaries: DISMISSED (5): ... / SHADOW: ... / NEEDS YOU: ...
    if (/^(DISMISSED \(\d+\):|SHADOW:|NEEDS YOU:)/.test(line)) {
      flushPre(); out.push(`<div class="dfoot">${esc(line)}</div>`); continue;
    }
    // bullets, with HIT./MISS. turned into chips
    if (/^[·\-•]\s/.test(line)) {
      let body = esc(line.slice(2))
        .replace(/^HIT\./, `<span class="chip hit">HIT</span>`)
        .replace(/^MISS\./, `<span class="chip miss">MISS</span>`);
      out.push(`<div class="dbullet">${body}</div>`);
      continue;
    }
    // ALLCAPS section headers, e.g. "CALLS (3)" or "UNEXPLAINED (2) <- ..."
    // (greedy caps run; bullets/footers/health were handled above)
    const hm = line.match(/^([A-Z][A-Z ]{3,})(.*)$/);
    if (hm) {
      flushPre();
      const name = hm[1].trim();
      const rm = hm[2].match(/^\s*(\(\d+\))?\s*(.*)$/);
      const count = rm[1] || "", note = rm[2] || "";
      const cls = name.startsWith("UNEXPLAINED") ? "sec-warn"
        : name.startsWith("CALLS") ? "sec-ok"
        : name.startsWith("RESOLVED") ? "sec-info" : "";
      out.push(`<h3 class="dsec ${cls}">${esc(name)}${count ? ` <span class="num">${esc(count)}</span>` : ""}` +
               `${note ? `<span class="dim"> ${esc(note)}</span>` : ""}</h3>`);
      continue;
    }
    // fixed-width table rows (2+ space column gaps) -> monospace block
    if (/\S {2,}\S/.test(line)) { pre.push(line); continue; }

    out.push(`<p class="dline">${esc(line)}</p>`);
  }
  flushPre();
  return out.join("\n");
}

function digestViewHTML(d) {
  if (!d || !d.available) {
    return `<div class="card stub">
      <h2>Digest</h2>
      <p>No digest file where serve.py looked for it
         (<code>--digest</code>, default <code>$DIGEST_OUT</code> or <code>digest.md</code>).</p>
      <p class="dim">It is the pipeline's published artifact — generate one with
         <code>python3 run.py digest --out digest.md</code>. The container healthcheck and your
         external watchdog watch its mtime; this view does too.</p>
    </div>`;
  }
  const stale = d.mtime && (Date.now() / 1000 - d.mtime > 36 * 3600);  // same rule as the Dockerfile healthcheck
  return `
  <div class="card digcard">
    <h2>digest.md
      <span class="dim num">written ${fmtUTC(d.mtime)} · ${fmtAgo(d.mtime)}</span>
      ${stale ? `<span class="chip invalid">stale &gt; 36h — watchdog territory</span>`
              : `<span class="chip active">fresh</span>`}
    </h2>
    ${renderDigest(d.text)}
  </div>`;
}

/* node smoke hook: render views without a DOM (see tests/ and README) */
if (typeof module !== "undefined" && module.exports) {
  module.exports = { overviewHTML, verdictBlock, freshnessTable, trackTable, alertsBlock,
                     feedsViewHTML, anomaliesHTML, ledgerHTML, eventsHTML, outcomeChip, sortBy,
                     digestViewHTML, renderDigest };
}
