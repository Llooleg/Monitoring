/* charts.js -- hand-rolled Canvas 2D chart for feed series. No dependencies.
 *
 * FeedChart(container, series) renders:
 *   - hourly values (line) at first-reported vintage
 *   - hour-of-week median ±3·MAD band (from /api/feed/N/series "band")
 *   - anomaly dots colored by triage verdict
 *   - weekend shading, time + value axes
 *   - coverage strip underneath
 *   - hover tooltip with crosshair (time, value, coverage, z + verdict)
 *
 * Self-contained on purpose: app.js only needs the global FeedChart.
 */
"use strict";

(function (global) {

const VERDICT_COLORS = {
  call: "#46b26b", noise: "#6b7688", unexplained: "#d9a13d",
  invalid: "#d95f5f", pending: "#8a93a6",
};
const TEXT = "#8a93a6", LINE = "#232b3a", ACCENT = "#5c9dd6";
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/* how = hour-of-week, Mon 00:00 UTC = 0 -- must match schema.sql's formula */
function howOf(t) {
  return (((Math.floor(t / 86400) + 3) % 7) * 24) + (Math.floor(t / 3600) % 24);
}

function fmtTick(t, withHour) {
  const d = new Date(t * 1000);
  const date = `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}`;
  if (!withHour) return date;
  return `${date} ${String(d.getUTCHours()).padStart(2, "0")}:00`;
}

function fmtTip(t) {
  const d = new Date(t * 1000);
  return `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()} ${String(d.getUTCHours()).padStart(2, "0")}:00Z`;
}

function niceTicks(min, max, count) {
  if (!(isFinite(min) && isFinite(max)) || min === max) {
    const pad = Math.abs(min) > 1 ? Math.abs(min) * 0.05 : 1;
    min -= pad; max += pad;
  }
  const span = max - min;
  const step0 = span / count;
  const mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const norm = step0 / mag;
  const step = (norm >= 5 ? 10 : norm >= 2 ? 5 : norm >= 1 ? 2 : 1) * mag;
  const out = [];
  for (let v = Math.ceil(min / step) * step; v <= max + 1e-9; v += step) out.push(v);
  return out;
}

function tickStepFor(rangeHours) {
  if (rangeHours <= 72) return { s: 8 * 3600, hour: true };
  if (rangeHours <= 336) return { s: 24 * 3600, hour: false };
  return { s: 7 * 86400, hour: false };
}

/* live instances; detached containers (view re-rendered) are pruned */
const instances = [];
if (typeof window !== "undefined") {
  let rto = null;
  window.addEventListener("resize", () => {
    clearTimeout(rto);
    rto = setTimeout(() => instances.forEach(c => c.container.isConnected && c.draw()), 100);
  });
}

class FeedChart {
  constructor(container, series) {
    this.container = container;
    this.series = series;
    container.classList.add("chart-wrap");
    container.innerHTML = "";

    this.cv = document.createElement("canvas");
    this.cv.className = "chart-main";
    this.cov = document.createElement("canvas");
    this.cov.className = "chart-cov";
    this.tip = document.createElement("div");
    this.tip.className = "tip";
    container.appendChild(this.cv);
    container.appendChild(this.cov);
    container.appendChild(this.tip);

    this.cv.addEventListener("mousemove", e => this.hover(e));
    this.cv.addEventListener("mouseleave", () => this.unhover());

    this.valueByT = new Map(series.points.map(p => [p[0], p[1]]));
    this.anomByT = new Map();
    (series.anomalies || []).forEach(a => this.anomByT.set(a.observed_at, a));

    instances.push(this);
    this.draw();
  }

  /* ------------------------------------------------------------ layout */

  sizes() {
    const w = Math.max(200, this.container.clientWidth || 600);
    const dpr = (typeof devicePixelRatio !== "undefined" && devicePixelRatio) || 1;
    this.mainG = { w, h: 190, ml: 46, mr: 10, mt: 8, mb: 20 };
    this.covG = { w, h: 34, ml: 46, mr: 10, mt: 4, mb: 12 };
    for (const [cv, g] of [[this.cv, this.mainG], [this.cov, this.covG]]) {
      cv.width = Math.round(g.w * dpr);
      cv.height = Math.round(g.h * dpr);
      cv.style.width = g.w + "px";
      cv.style.height = g.h + "px";
      const ctx = cv.getContext("2d");
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
  }

  scales() {
    const { points } = this.series;
    const g = this.mainG;
    const t0 = this.series.start, t1 = this.series.end;
    let vmin = Infinity, vmax = -Infinity;
    for (const p of points) { if (p[1] < vmin) vmin = p[1]; if (p[1] > vmax) vmax = p[1]; }
    if (!points.length) { vmin = 0; vmax = 1; }
    if (vmin === vmax) { const p = Math.max(1, Math.abs(vmin) * 0.1); vmin -= p; vmax += p; }
    const pad = (vmax - vmin) * 0.08;
    vmin -= pad; vmax += pad;
    this.t0 = t0; this.t1 = t1; this.vmin = vmin; this.vmax = vmax;
    this.x = t => g.ml + (t - t0) / (t1 - t0) * (g.w - g.ml - g.mr);
    this.y = v => g.mt + (1 - (v - vmin) / (vmax - vmin)) * (g.h - g.mt - g.mb);
    this.tx = px => t0 + (px - g.ml) / (g.w - g.ml - g.mr) * (t1 - t0);
  }

  /* -------------------------------------------------------------- draw */

  draw(overlay) {
    this.sizes();
    this.scales();
    this.drawMain(overlay);
    this.drawCoverage();
  }

  drawMain(overlay) {
    const ctx = this.cv.getContext("2d");
    const g = this.mainG, s = this.series;
    ctx.clearRect(0, 0, g.w, g.h);

    /* weekend shading (Sat/Sun UTC, matching the hour-of-week calendar) */
    ctx.fillStyle = "rgba(138,147,166,0.07)";
    for (let d = Math.floor(this.t0 / 86400) * 86400; d <= this.t1; d += 86400) {
      if (((d / 86400 + 3) % 7) >= 5) {
        ctx.fillRect(this.x(d), g.mt, this.x(d + 86400) - this.x(d), g.h - g.mt - g.mb);
      }
    }

    /* value grid + y labels */
    ctx.font = "10px ui-monospace, Menlo, monospace";
    for (const v of niceTicks(this.vmin, this.vmax, 4)) {
      const py = this.y(v);
      ctx.strokeStyle = LINE; ctx.beginPath();
      ctx.moveTo(g.ml, py); ctx.lineTo(g.w - g.mr, py); ctx.stroke();
      ctx.fillStyle = TEXT; ctx.textAlign = "right";
      ctx.fillText(String(Math.round(v * 100) / 100), g.ml - 5, py + 3);
    }

    /* time ticks */
    const { s: stepS, hour } = tickStepFor(s.range_hours || 336);
    ctx.textAlign = "center";
    for (let t = Math.ceil(this.t0 / stepS) * stepS; t <= this.t1; t += stepS) {
      const px = this.x(t);
      ctx.strokeStyle = LINE; ctx.beginPath();
      ctx.moveTo(px, g.mt); ctx.lineTo(px, g.h - g.mb); ctx.stroke();
      ctx.fillStyle = TEXT;
      ctx.fillText(fmtTick(t, hour), px, g.h - g.mb + 13);
    }

    /* hour-of-week band: median ±3·MAD */
    const band = new Map((s.band || []).map(b => [b.how, b]));
    const inBand = t => {
      const b = band.get(howOf(t));
      return b ? [b.median - 3 * b.mad, b.median + 3 * b.mad, b.median] : null;
    };
    const pts = s.points;
    if (pts.length) {
      ctx.beginPath();
      let pen = false;
      for (const p of pts) {
        const b = inBand(p[0]);
        if (!b) { pen = false; continue; }
        const py = this.y(Math.min(this.vmax, b[1]));
        pen ? ctx.lineTo(this.x(p[0]), py) : (ctx.moveTo(this.x(p[0]), py), pen = true);
      }
      for (let i = pts.length - 1; i >= 0; i--) {
        const b = inBand(pts[i][0]);
        if (!b) continue;
        ctx.lineTo(this.x(pts[i][0]), this.y(Math.max(this.vmin, b[0])));
      }
      ctx.closePath();
      ctx.fillStyle = "rgba(92,157,214,0.10)";
      ctx.fill();

      /* median line */
      ctx.beginPath(); pen = false;
      for (const p of pts) {
        const b = inBand(p[0]);
        if (!b) { pen = false; continue; }
        const px = this.x(p[0]), py = this.y(b[2]);
        pen ? ctx.lineTo(px, py) : (ctx.moveTo(px, py), pen = true);
      }
      ctx.strokeStyle = "rgba(92,157,214,0.55)";
      ctx.lineWidth = 1; ctx.stroke();
    }

    /* the series itself */
    ctx.beginPath();
    let pen = false;
    this.px = [];                       // for hit-testing
    for (const p of pts) {
      const px = this.x(p[0]), py = this.y(p[1]);
      this.px.push([px, p]);
      pen ? ctx.lineTo(px, py) : (ctx.moveTo(px, py), pen = true);
    }
    ctx.strokeStyle = "#d8dee9"; ctx.lineWidth = 1.4; ctx.stroke();

    /* anomaly dots at their observed values */
    for (const a of (s.anomalies || [])) {
      let v = this.valueByT.get(a.observed_at);
      if (v == null) {
        let best = null;
        for (const p of pts) if (Math.abs(p[0] - a.observed_at) <= 3600 && (best == null || Math.abs(p[0] - a.observed_at) < Math.abs(best[0] - a.observed_at))) best = p;
        if (!best) continue;
        v = best[1];
      }
      const px = this.x(a.observed_at), py = this.y(v);
      ctx.beginPath();
      ctx.arc(px, py, 3.6, 0, 2 * Math.PI);
      ctx.fillStyle = VERDICT_COLORS[a.verdict] || VERDICT_COLORS.pending;
      ctx.fill();
      ctx.strokeStyle = "#0b0e14"; ctx.lineWidth = 1.2; ctx.stroke();
    }

    /* hover overlay: crosshair + emphasized point */
    if (overlay && overlay.point) {
      const p = overlay.point;
      const px = this.x(p[0]), py = this.y(p[1]);
      ctx.strokeStyle = "rgba(138,147,166,0.5)";
      ctx.setLineDash([3, 3]); ctx.beginPath();
      ctx.moveTo(px, g.mt); ctx.lineTo(px, g.h - g.mb); ctx.stroke();
      ctx.setLineDash([]);
      ctx.beginPath(); ctx.arc(px, py, 4.5, 0, 2 * Math.PI);
      ctx.fillStyle = "#fff"; ctx.fill();
    }
  }

  drawCoverage() {
    const ctx = this.cov.getContext("2d");
    const g = this.covG, pts = this.series.points;
    ctx.clearRect(0, 0, g.w, g.h);
    ctx.font = "9px ui-monospace, Menlo, monospace";
    ctx.fillStyle = TEXT; ctx.textAlign = "right";
    ctx.fillText("cov", g.ml - 5, g.mt + 8);

    let cmax = 0;
    for (const p of pts) if (p[2] != null && p[2] > cmax) cmax = p[2];
    if (!cmax) return;
    const med = [];
    for (const p of pts) if (p[2] != null) med.push(p[2]);
    med.sort((a, b) => a - b);
    const cmed = med[Math.floor(med.length / 2)] || cmax;
    const cy = c => g.mt + (1 - c / (cmax * 1.1)) * (g.h - g.mt - g.mb);

    /* starvation regions: coverage under half the median */
    ctx.fillStyle = "rgba(217,161,61,0.18)";
    let open = false;
    for (const p of pts) {
      const starved = p[2] != null && p[2] < 0.5 * cmed;
      if (starved && !open) { open = true; ctx.fillRect(this.x(p[0]), g.mt, 1, g.h - g.mt - g.mb); }
      if (!starved) open = false;
    }

    ctx.beginPath();
    let pen = false;
    for (const p of pts) {
      if (p[2] == null) { pen = false; continue; }
      const px = this.x(p[0]), py = cy(p[2]);
      pen ? ctx.lineTo(px, py) : (ctx.moveTo(px, py), pen = true);
    }
    ctx.strokeStyle = "rgba(92,157,214,0.6)"; ctx.lineWidth = 1; ctx.stroke();
  }

  /* -------------------------------------------------------------- hover */

  hover(e) {
    const g = this.mainG;
    const rect = this.cv.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    if (!this.px || !this.px.length) return;
    let best = null, bd = Infinity;
    for (const [px, p] of this.px) {
      const d = Math.abs(px - mx);
      if (d < bd) { bd = d; best = p; }
    }
    if (!best || bd > 40) return this.unhover();

    const a = this.anomByT.get(best[0]) ||
      [...this.anomByT.entries()].find(([t]) => Math.abs(t - best[0]) <= 3600)?.[1];
    const z = a ? `<tr><td>z</td><td><b style="color:${VERDICT_COLORS[a.verdict] || "#fff"}">${a.z.toFixed(1)} · ${a.verdict}</b></td></tr>` : "";
    const sum = a && a.summary ? `<div class="tipsum">${a.summary}</div>` : "";
    this.tip.innerHTML =
      `<table><tr><td>at</td><td class="num">${fmtTip(best[0])}</td></tr>
       <tr><td>value</td><td class="num">${Math.round(best[1] * 100) / 100}</td></tr>
       <tr><td>cov</td><td class="num">${best[2] == null ? "—" : best[2]}</td></tr>${z}</table>${sum}`;
    this.tip.style.display = "block";
    const tw = this.tip.offsetWidth;
    const left = Math.max(2, Math.min(this.mainG.w - tw - 2, this.x(best[0]) + 10));
    this.tip.style.left = left + "px";
    this.tip.style.top = "6px";
    this.draw({ point: best });
  }

  unhover() {
    this.tip.style.display = "none";
    this.draw();
  }
}

global.FeedChart = FeedChart;
global.__charts_test = { howOf, niceTicks, tickStepFor, VERDICT_COLORS };  // node smoke hook

if (typeof module !== "undefined" && module.exports) {
  module.exports = { FeedChart, howOf, niceTicks, tickStepFor, VERDICT_COLORS };
}
})(typeof window !== "undefined" ? window : globalThis);
