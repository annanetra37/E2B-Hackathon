// Live tab: one real mini-substation, updated over Server-Sent Events.
const KIOSK = new URLSearchParams(location.search).get("kiosk") === "1";
const WINDOW_S = 120;
const ST_CLASS = { green: "ok", amber: "warning", red: "fault", grey: "offline" };
const ST_TEXT = { green: "Normal", amber: "High load", red: "UNUSUAL · POWER SPIKE", grey: "No data" };
const IDLE_W = 1;   // below this nothing is switched on

// What is plugged in, in plain words (the LED is the normal case, a heavy appliance is not)
function loadText(p) {
  if (typeof p !== "number") return "";
  if (S.status === "red") return "Unusual: something extra is drawing power";
  if (S.status === "amber") return "High load";
  if (p < IDLE_W) return "Nothing switched on";
  return "Normal use";
}

const $ = id => document.getElementById(id);
const nf0 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
const nf1 = new Intl.NumberFormat(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const nf2 = new Intl.NumberFormat(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmt = (v, f) => (typeof v === "number" && isFinite(v) ? f.format(v) : "–");
const timeOf = iso => clock(iso);      // station time zone (Armenia), see common.js

// on/off (DI) mode texts
const DI_TEXT = {
  green: "Supplying electricity",
  red: "There is a problem: the substation is not providing electricity",
  grey: "No data from gateway",
};
const DI_TEXT_HY = {
  green: "Էլեկտրաէներգիան մատակարարվում է",
  red: "Խնդիր կա. ենթակայանը էլեկտրաէներգիա չի մատակարարում",
  grey: "Գեյթվեյից տվյալներ չկան",
};
// DI_ALARM=overload: lamp ON (input 1) = overload, lamp OFF = normal
const OVL_TEXT = { green: "Normal operation", red: "There is a problem: the substation is overloaded", grey: "No data from gateway" };
const OVL_TEXT_HY = { green: "Նորմալ աշխատանք", red: "Խնդիր կա. ենթակայանը գերբեռնված է", grey: "Գեյթվեյից տվյալներ չկան" };
const isOverloadMode = () => S.diAlarm === "overload";
const DI_WINDOW_S = 600;
function fmtDur(sec) {
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return `${sec} s`;
  const m = Math.floor(sec / 60), s = sec % 60;
  if (m < 60) return s ? `${m} min ${s} s` : `${m} min`;
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}

const S = {
  signal: "power", diAlarm: "outage", supplyOn: null, confirmedSince: null, bridgeOnline: null, lastDi: null, diPoints: [],
  status: "grey", source: "live", station: null, thresholds: { warnW: 600, alarmW: 1000 },
  points: [],            // {t: ms (server time), p: W}
  lastSeenLocal: null,   // Date.now() when the last fresh reading arrived
  lastPower: null, skewMs: 0, events: [],
};

if (KIOSK) {
  document.body.classList.add("kiosk");
  $("qr-corner").hidden = false;
  document.addEventListener("click", () => {      // full screen needs a user gesture
    if (!document.fullscreenElement && document.documentElement.requestFullscreen) {
      document.documentElement.requestFullscreen().catch(() => {});
    }
  }, { once: true });
}

// ------------------------------------------------------------------ map
const map = L.map("map", { zoomControl: !KIOSK, attributionControl: true }).setView([40.1777, 44.5126], 12);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 18,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
}).addTo(map);
let marker = null;

// 0 W (nothing switched on) and no problem: a hollow dot, so "idle" reads differently from "on"
function isIdle() { return S.signal === "power" && S.status !== "grey" && typeof S.lastPower === "number" && S.lastPower < IDLE_W; }
function markerKey() { return `${S.status}${isIdle() ? "-idle" : ""}`; }

function markerIcon() {
  return L.divIcon({
    className: "",
    html: `<div class="live-marker st-${ST_CLASS[S.status]}${isIdle() ? " idle" : ""}"><span class="ring"></span><span class="ring r2"></span><span class="core"></span></div>`,
    iconSize: [34, 34], iconAnchor: [17, 17],
  });
}

function tooltipHtml() {
  const name = S.station ? esc(S.station.name) : "Demo station";
  if (S.signal === "di") {
    const t = S.status === "grey" ? "no data" : isOverloadMode() ? (S.status === "red" ? "OVERLOAD" : "Normal")
      : S.status === "red" ? "Electricity OFF" : "Electricity ON";
    return `<b>${name}</b><br>${t}`;
  }
  const p = S.status === "grey" ? "no data" : `${fmt(S.lastPower, nf0)} W`;
  return `<b>${name}</b><br>${p}`;
}

function renderMarker() {
  if (!S.station) return;
  if (!marker) {
    marker = L.marker([S.station.lat, S.station.lng], { icon: markerIcon(), title: S.station.name, keyboard: true })
      .addTo(map).on("click", toggleScheme);
    marker.bindTooltip(tooltipHtml(), { permanent: true, direction: "right", offset: [18, 0], className: "ss-label live-tt" });
    marker._st = markerKey();
    map.setView([S.station.lat, S.station.lng], 12);
  } else if (marker._st !== markerKey()) {
    marker.setIcon(markerIcon());
    marker._st = markerKey();
  }
  marker.setTooltipContent(tooltipHtml());
}

// ------------------------------------------------------------------ mini scheme (marker click)
function toggleScheme() { $("mini-scheme").hidden = !$("mini-scheme").hidden; renderScheme(); }
$("ms-close").addEventListener("click", () => { $("mini-scheme").hidden = true; });
function renderScheme() {
  const cls = ST_CLASS[S.status];
  $("ms-tag").setAttribute("class", `st-${cls}`);
  if (S.signal === "di") {
    const ovl = isOverloadMode();
    $("ms-status").textContent = S.status === "grey" ? "UNKNOWN" : ovl ? (S.status === "red" ? "OVERLOAD" : "NORMAL")
      : S.status === "green" ? "ON" : "OFF";
    $("ms-power").textContent = S.status === "grey" ? "no data" : ovl ? (S.status === "red" ? "Overloaded" : "Normal load")
      : S.status === "green" ? "Supplying electricity" : "No electricity";
    $("ms-feeder").setAttribute("class", S.status === "green" ? "cond kv04" : "cond dead");
  } else {
    $("ms-status").textContent = ST_TEXT[S.status];
    $("ms-power").textContent = S.status === "grey" ? "no data" : `${fmt(S.lastPower, nf0)} W`;
    $("ms-feeder").setAttribute("class", S.status === "grey" ? "cond dead" : "cond kv04");
  }
  if (S.station) $("ms-title").textContent = S.station.name;
}

// ------------------------------------------------------------------ status, tiles
function renderStatus() {
  const cls = ST_CLASS[S.status] || "offline";
  const b = $("banner");
  b.className = `status-banner st-${cls}`;
  if (S.signal === "di") {
    const [en, hy] = isOverloadMode() ? [OVL_TEXT, OVL_TEXT_HY] : [DI_TEXT, DI_TEXT_HY];
    $("banner-text").innerHTML = `${esc(en[S.status] || "")}<span class="hy" lang="hy">${esc(hy[S.status] || "")}</span>`;
    renderDi();
  } else {
    let text = ST_TEXT[S.status];
    if (S.status === "red" && typeof S.lastPower === "number") text += ` · ${fmt(S.lastPower, nf0)} W`;
    $("banner-text").textContent = text;
  }
  document.body.dataset.status = S.status;
  $("sim-label").hidden = S.source !== "simulator";
  renderMarker();
  renderScheme();
  if (typeof S.lastPower === "number") $("t-load").textContent = loadText(S.lastPower);
}

// ------------------------------------------------------------------ on/off (DI) layout
function applyLayout() {
  const di = S.signal === "di";
  $("di-box").hidden = !di; $("di-chart-box").hidden = !di;
  $("power-tiles").hidden = di; $("power-chart-box").hidden = di;
  document.body.classList.toggle("di-mode", di);
}

function renderDi() {
  const ovl = isOverloadMode();
  // S.supplyOn = "all is well" (true) / "alarm" (false) as confirmed by the server, in both modes
  const good = S.status === "grey" ? null : S.supplyOn;
  const v = good == null ? "UNKNOWN" : ovl ? (good ? "NORMAL" : "OVERLOAD") : (good ? "ON" : "OFF");
  const cls = good == null ? "unknown" : good ? "on" : "off";
  const el = $("di-val");
  el.textContent = v;
  el.className = `tile-val di-val di-${cls}`;
  $("di-label").textContent = ovl ? "Load · Բեռնվածություն" : "Electricity · Էլեկտրաէներգիա";
  let since = "";
  if (S.confirmedSince && S.status !== "grey") {
    const t = new Date(S.confirmedSince).getTime();
    since = `since ${timeOf(S.confirmedSince)} (${fmtDur((Date.now() + S.skewMs - t) / 1000)})`;
  } else if (S.status === "grey") {
    const last = S.supplyOn == null ? null : ovl ? (S.supplyOn ? "NORMAL" : "OVERLOAD") : (S.supplyOn ? "ON" : "OFF");
    since = last == null ? "state unknown" : `state unknown · last known: ${last}`;
  }
  $("di-since").textContent = since;
  $("di-input").textContent = S.lastDi == null ? "" :
    `Gateway input: di1 = ${S.lastDi ? 1 : 0}${ovl ? ` (light ${S.lastDi ? "on" : "off"})` : ""}`;
  $("di-chart-title").textContent = ovl ? "Load, last 10 minutes" : "Electricity, last 10 minutes";
  $("lg-good").textContent = ovl ? "Normal" : "On"; $("lg-bad").textContent = ovl ? "Overload" : "Off";
  $("di-bridge").textContent = S.source === "simulator" ? "Simulator is on air"
    : S.bridgeOnline === true ? "DI bridge online" : S.bridgeOnline === false ? "DI bridge OFFLINE" : "";
  $("di-bridge").className = `di-bridge ${S.bridgeOnline === false && S.source !== "simulator" ? "bad" : ""}`;
}

function applyStatus(s) {
  S.status = s.status; S.source = s.source;
  if (s.signal) S.signal = s.signal;
  if (s.diAlarm) S.diAlarm = s.diAlarm;
  if ("supplyOn" in s) S.supplyOn = s.supplyOn;
  if ("confirmedSince" in s) S.confirmedSince = s.confirmedSince;
  if ("bridgeOnline" in s) S.bridgeOnline = s.bridgeOnline;
  applyLayout();
}

function renderReading(r) {
  if (S.signal === "di") {
    if (typeof r.supplyOn === "boolean") S.lastDi = r.supplyOn;
    renderDi();
    if (marker) renderMarker();
    return;
  }
  S.lastPower = r.powerW;
  $("t-power").textContent = typeof r.powerW === "number" && r.powerW < 100 ? fmt(r.powerW, nf1) : fmt(r.powerW, nf0);
  $("t-load").textContent = loadText(r.powerW);
  if (S.status === "red") $("banner-text").textContent = `${ST_TEXT.red} · ${fmt(r.powerW, nf0)} W`;
  $("t-current").textContent = fmt(r.currentA, nf2);
  $("t-voltage").textContent = fmt(r.voltageV, nf1);
  $("t-pf").textContent = fmt(r.pf, nf2);
  $("t-freq").textContent = fmt(r.freqHz, nf2);
  if (marker) renderMarker();
  if (!$("mini-scheme").hidden) renderScheme();
}

function renderUpdated() {
  const el = $("updated");
  if (S.lastSeenLocal == null) { el.textContent = "No data yet"; return; }
  const s = Math.max(0, Math.round((Date.now() - S.lastSeenLocal) / 1000));
  el.textContent = s <= 1 ? "Updated just now" : `Updated ${nf0.format(s)} s ago`;
  el.classList.toggle("stale", S.status === "grey");
}

// ------------------------------------------------------------------ events
function eventText(e) {
  const sms = e.smsSent ? ` · SMS to ${e.smsSent} phone${e.smsSent === 1 ? "" : "s"}` : "";
  if (S.signal === "di") {
    if (e.toState === "red" && e.fromState !== "red") {
      const lasted = e.durationS != null ? ` · lasted ${fmtDur(e.durationS)}` : (e.id === S.events.find(x => x.toState === "red")?.id && S.status === "red" ? " · ongoing" : "");
      return `${e.message || (isOverloadMode() ? "Overload" : "Supply lost")}${lasted}${sms}`;
    }
    return `${e.message || e.toState}${sms}`;
  }
  if (e.toState === "red") {
    let t = "Power spike";
    const usual = /usual ([\d,.]+) W/.exec(e.message || "");
    if (usual) t += ` (usual ${usual[1]} W)`;
    if (e.peakW) t += ` · peak ${nf0.format(e.peakW)} W`;
    if (e.smsSent) t += ` · SMS to ${e.smsSent} phone${e.smsSent === 1 ? "" : "s"}`;
    return t;
  }
  let t = e.message || `${e.fromState} → ${e.toState}`;
  if (e.smsSent) t += ` · SMS to ${e.smsSent} phone${e.smsSent === 1 ? "" : "s"}`;
  return t;
}

function renderEvents() {
  const list = $("events");
  if (!S.events.length) { list.innerHTML = `<li><span></span><span class="hint">No events yet.</span></li>`; return; }
  list.innerHTML = S.events.slice(0, 10).map(e => `
    <li class="st-${ST_CLASS[e.toState] || "offline"}"><span class="ev-time">${timeOf(e.ts)}</span>
    <span class="ev-text"><span class="mark"></span>${esc(eventText(e))}</span></li>`).join("");
}

function upsertEvent(e) {
  const i = S.events.findIndex(x => x.id === e.id);
  if (i >= 0) S.events[i] = e; else S.events.unshift(e);
  S.events.sort((a, b) => (b.ts > a.ts ? 1 : b.ts < a.ts ? -1 : b.id - a.id));
  S.events = S.events.slice(0, 10);
  renderEvents();
}

// ------------------------------------------------------------------ chart (canvas, append-only data)
const canvas = $("chart");
const ctx = canvas.getContext("2d");
let dirty = true;

function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

function drawChart() {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const padL = 46, padR = 8, padT = 8, padB = 20;
  const now = Date.now() + S.skewMs;
  const t0 = now - WINDOW_S * 1000;
  const pts = S.points.filter(p => p.t >= t0 - 2000);
  const maxP = Math.max(S.thresholds.alarmW * 1.3, ...pts.map(p => p.p * 1.1), 100);
  const x = t => padL + (t - t0) / (WINDOW_S * 1000) * (w - padL - padR);
  const y = p => padT + (1 - p / maxP) * (h - padT - padB);

  ctx.font = `12px ${css("font-family") || "sans-serif"}`;
  ctx.fillStyle = css("--muted"); ctx.strokeStyle = css("--rule"); ctx.lineWidth = 1;
  const step = maxP > 3000 ? 1000 : 500;
  for (let v = 0; v <= maxP; v += step) {
    ctx.beginPath(); ctx.moveTo(padL, y(v)); ctx.lineTo(w - padR, y(v)); ctx.stroke();
    ctx.fillText(nf0.format(v), 4, y(v) + 4);
  }
  ctx.fillText("−2 min", padL, h - 4);
  ctx.textAlign = "right"; ctx.fillText("now", w - padR, h - 4); ctx.textAlign = "left";

  for (const [val, color] of [[S.thresholds.warnW, css("--warn")], [S.thresholds.alarmW, css("--fault")]]) {
    ctx.save(); ctx.setLineDash([6, 4]); ctx.strokeStyle = color; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(padL, y(val)); ctx.lineTo(w - padR, y(val)); ctx.stroke(); ctx.restore();
  }

  if (pts.length) {
    ctx.save();
    ctx.beginPath(); ctx.rect(padL, 0, w - padL - padR, h); ctx.clip();
    ctx.beginPath();
    pts.forEach((p, i) => (i ? ctx.lineTo(x(p.t), y(p.p)) : ctx.moveTo(x(p.t), y(p.p))));
    ctx.strokeStyle = css("--ink"); ctx.lineWidth = 2; ctx.lineJoin = "round"; ctx.stroke();
    ctx.lineTo(x(pts[pts.length - 1].t), y(0)); ctx.lineTo(x(pts[0].t), y(0)); ctx.closePath();
    ctx.fillStyle = S.status === "red" ? "rgba(207,51,40,0.14)" : "rgba(29,43,51,0.07)"; ctx.fill();
    ctx.restore();
  }
  $("lg-warn-v").textContent = `${nf0.format(S.thresholds.warnW)} W`;
  $("lg-alarm-v").textContent = `${nf0.format(S.thresholds.alarmW)} W`;
}

// on/off step chart: green = on, red = off, blank = no data (gap longer than the stale time)
const diCanvas = $("di-chart");
const dctx = diCanvas.getContext("2d");
function drawDiChart() {
  const dpr = window.devicePixelRatio || 1;
  const w = diCanvas.clientWidth, h = diCanvas.clientHeight;
  if (!w) return;
  if (diCanvas.width !== Math.round(w * dpr) || diCanvas.height !== Math.round(h * dpr)) {
    diCanvas.width = Math.round(w * dpr); diCanvas.height = Math.round(h * dpr);
  }
  dctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  dctx.clearRect(0, 0, w, h);
  const padL = 34, padR = 8, padT = 10, padB = 20;
  const now = Date.now() + S.skewMs, t0 = now - DI_WINDOW_S * 1000;
  const x = t => padL + Math.max(0, t - t0) / (DI_WINDOW_S * 1000) * (w - padL - padR);
  const yOn = padT, yOff = padT + (h - padT - padB) * 0.55, base = h - padB;
  dctx.font = `12px ${css("font-family") || "sans-serif"}`;
  dctx.fillStyle = css("--muted");
  const [goodLbl, badLbl] = isOverloadMode() ? ["OK", "OVL"] : ["ON", "OFF"];
  dctx.fillText(goodLbl, 4, yOn + 12); dctx.fillText(badLbl, 4, yOff + 12);
  dctx.fillText("−10 min", padL, h - 4);
  dctx.textAlign = "right"; dctx.fillText("now", w - padR, h - 4); dctx.textAlign = "left";
  dctx.strokeStyle = css("--rule"); dctx.beginPath(); dctx.moveTo(padL, base); dctx.lineTo(w - padR, base); dctx.stroke();
  const gap = Math.max(3, (window.STALE_S || 10)) * 1000;
  // merge readings into runs of the same value; a silence longer than the stale time ends a run
  const pts = S.diPoints.filter(p => p.t >= t0 - gap).sort((a, b) => a.t - b.t);
  const runs = [];
  for (let i = 0; i < pts.length; i++) {
    const a = pts[i], next = pts[i + 1];
    let end = next ? Math.min(next.t, a.t + gap) : Math.min(now, a.t + gap);
    const last = runs[runs.length - 1];
    if (last && last.on === a.on && a.t - last.end < 1) last.end = end;
    else runs.push({ on: a.on, start: a.t, end });
  }
  for (const r of runs) {
    if (r.end <= t0) continue;
    const xa = Math.round(x(Math.max(r.start, t0))), xb = Math.round(x(r.end));
    const top = r.on ? yOn : yOff;
    dctx.fillStyle = r.on ? css("--ok") : css("--fault");
    dctx.fillRect(xa, top, Math.max(2, xb - xa), base - top);
  }
}
function addDiPoint(r) {
  if (typeof r.supplyOn !== "boolean") return;
  // chart rows: top/green = all is well, bottom/red = alarm (overload mode: lamp ON is the alarm)
  S.diPoints.push({ t: new Date(r.ts).getTime(), on: isOverloadMode() ? !r.supplyOn : r.supplyOn });
  const cutoff = Date.now() + S.skewMs - (DI_WINDOW_S + 30) * 1000;
  while (S.diPoints.length && S.diPoints[0].t < cutoff) S.diPoints.shift();
  dirty = true;
}

function addPoint(r) {
  if (typeof r.supplyOn === "boolean") { addDiPoint(r); return; }
  if (typeof r.powerW !== "number") return;
  S.points.push({ t: new Date(r.ts).getTime(), p: r.powerW });
  const cutoff = Date.now() + S.skewMs - (WINDOW_S + 10) * 1000;
  while (S.points.length && S.points[0].t < cutoff) S.points.shift();
  dirty = true;
}

function frame() {
  if (dirty) { if (S.signal === "di") drawDiChart(); else drawChart(); dirty = false; }
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
setInterval(() => { dirty = true; renderUpdated(); if (S.signal === "di") renderDi(); }, 500);     // scroll the time axis, refresh "n s ago"
window.addEventListener("resize", () => { dirty = true; });

// ------------------------------------------------------------------ data loading + SSE
async function loadAll() {
  const st = await getJSON("/api/live/state");
  if (st.timeZone) window.APP_TZ = st.timeZone;
  applyStatus(st);
  window.STALE_S = st.staleAfterS;
  const [hist, ev] = await Promise.all([
    getJSON(`/api/live/history?seconds=${S.signal === "di" ? DI_WINDOW_S : WINDOW_S}`), getJSON("/api/live/events?limit=10"),
  ]);
  S.skewMs = new Date(st.serverTime).getTime() - Date.now();
  S.station = st.station; S.thresholds = st.thresholds;
  $("station-name").textContent = st.station.name;
  document.title = `Live · ${st.station.name}`;
  if (st.lastReading) {
    renderReading(st.lastReading);
    S.lastSeenLocal = new Date(st.lastReading.ts).getTime() - S.skewMs;
  }
  S.points = []; S.diPoints = [];
  hist.readings.filter(r => !r.late).forEach(addPoint);
  S.events = ev.events;
  renderEvents(); renderStatus(); renderUpdated();
}

let es = null, retry = 0, retryTimer = null;

function setConn(ok, text) {
  $("conn").className = `conn ${ok ? "on" : "off"}`;
  $("conn-text").textContent = text;
}

function connect() {
  clearTimeout(retryTimer);
  if (es) es.close();
  es = new EventSource("/api/live/stream");
  es.addEventListener("open", async () => {
    setConn(true, "Live");
    if (retry > 0) { try { await loadAll(); } catch (e) { /* next message will fix it */ } }
    retry = 0;
  });
  es.addEventListener("reading", m => {
    const r = JSON.parse(m.data);
    if (r.late) return;                         // cache back-fill: stored, but not "live"
    S.lastSeenLocal = Date.now();
    renderReading(r); addPoint(r); renderUpdated();
  });
  es.addEventListener("status", m => {
    applyStatus(JSON.parse(m.data));
    renderStatus(); renderEvents(); dirty = true;
  });
  es.addEventListener("event", m => upsertEvent(JSON.parse(m.data)));
  es.addEventListener("sms", m => window.showSmsPreview && showSmsPreview(JSON.parse(m.data)));
  es.addEventListener("thresholds", m => { S.thresholds = JSON.parse(m.data); dirty = true; renderStatus(); });
  es.addEventListener("error", () => {
    es.close();
    const delay = Math.min(30000, 1000 * 2 ** retry++);
    setConn(false, `Reconnecting in ${Math.round(delay / 1000)} s…`);
    retryTimer = setTimeout(connect, delay);
  });
}

(async function start() {
  try { await loadAll(); } catch (e) { retry = 1; setConn(false, "Can't reach the server. Retrying…"); }
  connect();
})();
