// Substation page: live single-line diagram + details, trend and recent changes.
// Two drawings: "primary" (110/10 kV) and "distribution" (the hand-drawn sketch, -> 0.4 kV).
const SID = decodeURIComponent(location.pathname.split("/").filter(Boolean).pop());
let snap = null;
let selectedId = null;
let demoReady = false;
let lastTrendAt = 0;

const KV_CLASS = kv => (kv >= 100 ? "kv110" : kv >= 30 ? "kv35" : kv >= 6 ? "kv10" : "kv04");
const KV_NAME = { kv110: "110 kV", kv35: "35 kV", kv10: "10 kV", kv04: "0.4 kV" };
const TAG_W = 196;

// ------------------------------------------------------------------ helpers
const getVal = (e, label) => {
  const v = e && e.values.find(x => x.label === label);
  return v ? v.value : undefined;
};
const cut = (s, n) => (s.length > n ? s.slice(0, n - 1) + "…" : s);
const cond = (x1, y1, x2, y2, live, kvc, extra = "") =>
  `<line class="cond ${live ? kvc : "dead"} ${extra}" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"/>`;

function halo(e, x, y, w, h) {
  if (!["fault", "warning", "offline"].includes(e.status)) return "";
  return `<rect class="halo" x="${x}" y="${y}" width="${w}" height="${h}" rx="6"/>`;
}

// value tag next to an element: title, working / not working, values
function tag(e, x, y, w = TAG_W) {
  const vals = e.values.slice(0, 5);
  const h = 46 + vals.length * 17 + (vals.length ? 4 : 0);
  let s = `<g class="tag" transform="translate(${x},${y})">
    <rect class="tag-box" width="${w}" height="${h}" rx="4"/>
    <rect class="tag-stripe" width="5" height="${h}" rx="2"/>
    <text class="tag-title" x="14" y="20">${esc(cut(e.label, Math.floor(w / 8.4)))}</text>
    <text class="tag-status" x="14" y="38">${STATUS_WORD[e.status] || e.status}</text>`;
  vals.forEach((v, i) => {
    s += `<text class="tag-val" x="14" y="${58 + i * 17}">${esc(v.label)}
      <tspan class="num" x="${w - 12}" text-anchor="end">${esc(fmtValue(v.value))}${v.unit ? `<tspan class="u"> ${esc(v.unit)}</tspan>` : ""}</tspan></text>`;
  });
  return { svg: s + "</g>", h };
}

function group(e, inner) {
  const label = `${e.label}: ${STATUS_WORD[e.status] || e.status}${e.message ? ". " + e.message : ""}`;
  return `<g class="el st-${e.status}${e.id === selectedId ? " selected" : ""}" data-id="${esc(e.id)}"
            tabindex="0" role="button" aria-label="${esc(label)}">${inner}</g>`;
}

const arrowDown = (x, y, live, kvc) =>
  `<path class="arrow ${live ? kvc : "dead"}" d="M${x - 7} ${y} L${x + 7} ${y} L${x} ${y + 11} Z"/>`;

function breakerSym(x, cy, closed) {
  const cls = closed === true ? "closed" : closed === false ? "open" : "unknown";
  let s = `<rect class="breaker ${cls}" x="${x - 11}" y="${cy - 11}" width="22" height="22" rx="2"/>`;
  if (closed === false) s += `<path class="open-mark" d="M${x - 6} ${cy - 6} L${x + 6} ${cy + 6} M${x + 6} ${cy - 6} L${x - 6} ${cy + 6}"/>`;
  return s;
}

function transformerSym(x, cy, kvTop, liveTop, kvBottom, liveBottom) {
  return `<circle class="tr ${liveTop ? kvTop : "dead"}" cx="${x}" cy="${cy - 15}" r="20"/>
          <circle class="tr ${liveBottom ? kvBottom : "dead"}" cx="${x}" cy="${cy + 15}" r="20"/>`;
}

// ------------------------------------------------------------------ primary 110/10 kV
function drawPrimary(E) {
  const feeders = snap.elements.filter(e => e.kind === "feeder");
  const x0 = 230, start = 150, gap = 210;
  const W = Math.max(920, start + (feeders.length - 1) * gap + 160);
  const line = E.LINE, q0 = E.Q0, t1 = E.T1, q1 = E.Q1, bus = E.BUS;
  const lineLive = line.status !== "offline" && (getVal(line, "Voltage") || 0) > 1;
  const trLive = lineLive && q0.closed !== false;
  const busLive = bus.status !== "offline" && (getVal(bus, "Voltage") || 0) > 1;
  let s = `<text class="diagram-text" x="${x0}" y="18" text-anchor="middle">110 kV grid</text>`;
  const parts = [];

  // incoming line + meter
  s += cond(x0, 38, x0, 119, lineLive, "kv110");
  let t = tag(line, x0 + 44, 72 - 50);
  parts.push(group(line, halo(line, x0 - 18, 26, 36, 70) + arrowDown(x0, 26, lineLive, "kv110") +
    `<circle class="meter" cx="${x0}" cy="72" r="9"/><text class="meter-t" x="${x0}" y="76" text-anchor="middle">V</text>` + t.svg));
  // Q0
  t = tag(q0, x0 - 44 - 170, 130 - 30, 170);
  parts.push(group(q0, halo(q0, x0 - 18, 112, 36, 36) + breakerSym(x0, 130, q0.closed) + t.svg));
  s += cond(x0, 141, x0, 172, trLive, "kv110");
  // T1
  t = tag(t1, x0 + 44, 205 - 52);
  parts.push(group(t1, halo(t1, x0 - 28, 162, 56, 86) + transformerSym(x0, 205, "kv110", trLive, "kv10", trLive) + t.svg));
  s += cond(x0, 240, x0, 285, trLive, "kv10");
  // Q1
  t = tag(q1, x0 - 44 - 170, 296 - 30, 170);
  parts.push(group(q1, halo(q1, x0 - 18, 278, 36, 36) + breakerSym(x0, 296, q1.closed) + t.svg));
  s += cond(x0, 307, x0, 345, busLive, "kv10");
  // busbar
  t = tag(bus, W - 40 - 220, 0, 220);
  const busTag = tag(bus, W - 40 - 220, 345 - 16 - t.h, 220);
  parts.push(group(bus, halo(bus, 50, 335, W - 80, 20) +
    `<line class="cond bus ${busLive ? "kv10" : "dead"}" x1="60" y1="345" x2="${W - 40}" y2="345"/>` + busTag.svg));

  // feeders
  let maxH = 0;
  feeders.forEach((f, i) => {
    const fx = start + i * gap;
    const live = f.energised !== false && busLive && f.status !== "offline";
    s += cond(fx, 349, fx, 395, busLive, "kv10");
    t = tag(f, fx - 88, 492, 176);
    maxH = Math.max(maxH, t.h);
    parts.push(group(f, halo(f, fx - 18, 388, 36, 98) + breakerSym(fx, 406, f.closed) +
      cond(fx, 417, fx, 468, live, "kv10") + arrowDown(fx, 468, live, "kv10") + t.svg));
  });
  return { s, parts, W, bottom: 492 + maxH, kvs: ["kv110", "kv10"], x0, busY: 345 };
}

// ------------------------------------------------------------------ distribution (the sketch)
function disconnectorSym(x, top, bottom, closed) {
  let s = `<line class="contact" x1="${x - 8}" y1="${top}" x2="${x + 8}" y2="${top}"/>
           <circle class="hinge" cx="${x}" cy="${bottom}" r="3.5"/>`;
  if (closed === false) s += `<line class="blade open" x1="${x}" y1="${bottom}" x2="${x + 17}" y2="${top + 4}"/>`;
  else s += `<line class="blade ${closed === true ? "" : "unknown"}" x1="${x}" y1="${bottom}" x2="${x}" y2="${top}"/>`;
  return s;
}

function fusesSym(x, cy, blown, live, kvc) {
  const xs = [x - 20, x, x + 20];
  let s = `<line class="cond ${live ? kvc : "dead"} thin" x1="${xs[0]}" y1="${cy - 22}" x2="${xs[2]}" y2="${cy - 22}"/>
           <line class="cond ${live ? kvc : "dead"} thin" x1="${xs[0]}" y1="${cy + 22}" x2="${xs[2]}" y2="${cy + 22}"/>`;
  xs.forEach((px, i) => {
    const b = blown && blown[i];
    s += `<line class="cond ${live && !b ? kvc : "dead"} thin" x1="${px}" y1="${cy - 22}" x2="${px}" y2="${cy + 22}"/>
          <rect class="fuse${b ? " blown" : ""}" x="${px - 5}" y="${cy - 13}" width="10" height="26" rx="1.5"/>
          <text class="phase-t" x="${px + 7}" y="${cy + 4}">${i + 1}</text>`;
    if (b) s += `<path class="open-mark" d="M${px - 7} ${cy - 7} L${px + 7} ${cy + 7} M${px + 7} ${cy - 7} L${px - 7} ${cy + 7}"/>`;
  });
  return s;
}

function drawDistribution(E) {
  const BUS_Y = 390;
  const lines = snap.elements.filter(e => e.kind === "feeder");
  const x0 = 230, start = 150, gap = 210;
  const W = Math.max(920, start + (lines.length - 1) * gap + 160);
  const line = E.LINE, qs = E.QS, fu = E.FU, tr = E.T, bus = E.BUS;
  const mvc = KV_CLASS(line.kv || 10);
  const lineLive = line.energised !== false && line.status !== "offline";
  const afterQs = lineLive && qs.closed !== false;
  const alive = bus.phases_alive || [false, false, false];
  const busLive = bus.status !== "offline" && alive.some(Boolean);
  let s = `<text class="diagram-text" x="${x0}" y="18" text-anchor="middle">${esc(line.kv)} kV line</text>`;
  const parts = [];

  // incoming line (judged from the 0.4 kV side)
  s += cond(x0, 38, x0, 112, lineLive, mvc);
  let t = tag(line, x0 + 44, 30);
  parts.push(group(line, halo(line, x0 - 18, 24, 36, 84) + arrowDown(x0, 26, lineLive, mvc) + t.svg));
  // QS disconnector
  t = tag(qs, x0 - 44 - 170, 128 - 34, 170);
  parts.push(group(qs, halo(qs, x0 - 18, 104, 36, 48) + disconnectorSym(x0, 112, 144, qs.closed) + t.svg));
  s += cond(x0, 148, x0, 183, afterQs, mvc);
  // FU fuses (one per phase)
  t = tag(fu, x0 - 44 - 170, 172, 170);
  parts.push(group(fu, halo(fu, x0 - 32, 176, 66, 58) + fusesSym(x0, 205, fu.blown, afterQs, mvc) + t.svg));
  s += cond(x0, 227, x0, 265, afterQs, mvc);
  // transformer (with temperature, "T = 63" in the sketch)
  t = tag(tr, x0 + 44, 300 - 46);
  parts.push(group(tr, halo(tr, x0 - 28, 257, 56, 86) + transformerSym(x0, 300, mvc, afterQs, "kv04", busLive) + t.svg));
  s += cond(x0, 335, x0, BUS_Y, busLive, "kv04");
  // 0.4 kV busbar with the L1/L2/L3 box (230 / 241 in the sketch)
  t = tag(bus, 0, 0, 220);
  const busTag = tag(bus, W - 40 - 220, BUS_Y - 16 - t.h, 220);
  parts.push(group(bus, halo(bus, 50, BUS_Y - 10, W - 80, 20) +
    `<line class="cond bus ${busLive ? "kv04" : "dead"}" x1="60" y1="${BUS_Y}" x2="${W - 40}" y2="${BUS_Y}"/>` + busTag.svg));

  // outgoing lines: circles = current transformers (measured), blade = plain switch
  let maxH = 0;
  const TY = BUS_Y + 150;
  lines.forEach((f, i) => {
    const fx = start + i * gap;
    const live = busLive && f.energised !== false;
    let sym = cond(fx, BUS_Y + 4, fx, BUS_Y + 125, live, "kv04");
    const cy = BUS_Y + 55;
    if (f.measured) sym += `<circle class="ct" cx="${fx}" cy="${cy - 8}" r="12"/><circle class="ct" cx="${fx}" cy="${cy + 8}" r="12"/>`;
    else sym += `<rect class="gap" x="${fx - 6}" y="${cy - 16}" width="12" height="32"/>` + disconnectorSym(fx, cy - 14, cy + 14, true);
    t = tag(f, fx - 88, TY, 176);
    maxH = Math.max(maxH, t.h);
    parts.push(group(f, halo(f, fx - 20, BUS_Y + 30, 40, 112) + sym + arrowDown(fx, BUS_Y + 125, live, "kv04") + t.svg));
  });
  return { s, parts, W, bottom: TY + maxH, kvs: [mvc, "kv04"], x0, busY: BUS_Y };
}

// ------------------------------------------------------------------ whole diagram
function drawDiagram() {
  const svg = document.getElementById("sld");
  const focusedId = svg.contains(document.activeElement) ? document.activeElement.dataset.id : null;
  const E = Object.fromEntries(snap.elements.map(e => [e.id, e]));
  if (!snap.elements.length) {
    svg.setAttribute("viewBox", "0 0 600 80");
    svg.setAttribute("width", 600);
    svg.innerHTML = `<text class="diagram-text" x="300" y="44" text-anchor="middle">Waiting for the first data from this substation…</text>`;
    return;
  }
  const d = snap.type === "primary" ? drawPrimary(E) : drawDistribution(E);

  // data box (gateway) at the bottom, linked by a dashed signal line
  const gw = E.GW;
  const gy = d.bottom + 34;
  const t = tag(gw, d.x0 - 98, gy, 196);
  const gwPart = group(gw, halo(gw, d.x0 - 104, gy - 6, 208, t.h + 12) + t.svg);
  const signal = `<path class="signal" d="M${d.x0 - 98} ${gy + t.h / 2} H36 V${d.busY} H60"/>
    <text class="diagram-text" x="44" y="${gy + t.h / 2 - 8}">data</text>`;
  const H = gy + t.h + 20;

  svg.setAttribute("viewBox", `0 0 ${d.W} ${H}`);
  svg.setAttribute("width", d.W);
  svg.setAttribute("height", H);
  svg.innerHTML = d.s + signal + d.parts.join("") + gwPart;

  document.getElementById("legend").innerHTML =
    d.kvs.map(k => `<span><i class="lg-${k}"></i>${KV_NAME[k]} energised</span>`).join("") +
    `<span><i class="lg-dead"></i>No voltage or no data</span>
     <span><b class="lg-br closed"></b>Breaker closed</span><span><b class="lg-br open"></b>Breaker open</span>
     ${snap.type === "distribution" ? `<span><b class="lg-ct"></b>Line with current meter</span>` : ""}`;

  if (focusedId) {
    const again = svg.querySelector(`.el[data-id="${CSS.escape(focusedId)}"]`);
    if (again) again.focus({ preventScroll: true });
  }
}

// ------------------------------------------------------------------ side panel
function sparkline(series) {
  const pts = series.points.map(p => p[1]);
  const min = Math.min(...pts), max = Math.max(...pts);
  const span = max - min || Math.abs(max) * 0.05 || 1;
  const W = 300, H = 64, pad = 6;
  const xy = pts.map((v, i) => [pad + (i / Math.max(1, pts.length - 1)) * (W - 2 * pad),
                                H - pad - ((v - min) / span) * (H - 2 * pad)]);
  const last = xy[xy.length - 1];
  return `<figure>
    <figcaption><span>${esc(series.metric)}</span><span><b>${fmtValue(pts[pts.length - 1])}</b> ${esc(series.unit)}</span></figcaption>
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img"
         aria-label="${esc(series.metric)} over the last 30 minutes, from ${fmtValue(min)} to ${fmtValue(max)} ${esc(series.unit)}">
      <polyline points="${xy.map(p => p.map(n => n.toFixed(1)).join(",")).join(" ")}"/>
      <circle cx="${last[0].toFixed(1)}" cy="${last[1].toFixed(1)}" r="3"/>
    </svg>
    <span class="range">${fmtValue(min)} to ${fmtValue(max)} ${esc(series.unit)} in the last 30 minutes</span>
  </figure>`;
}

async function loadTrend(id) {
  const box = document.getElementById("trend");
  if (!box) return;
  lastTrendAt = Date.now();
  try {
    const series = (await getJSON(`/api/substations/${encodeURIComponent(SID)}/history?element=${encodeURIComponent(id)}&minutes=30`))
      .filter(s => s.points.length > 1);
    if (id !== selectedId) return;
    box.innerHTML = series.length ? series.map(sparkline).join("")
      : `<p class="hint">The trend appears after the first minute of data.</p>`;
  } catch { box.innerHTML = `<p class="hint">Trend not available right now.</p>`; }
}

function renderDetails() {
  const box = document.getElementById("details");
  const e = snap.elements.find(x => x.id === selectedId);
  if (!e) {
    box.innerHTML = `<p class="hint">Select any part of the diagram to see its details and trend.</p>`;
    return;
  }
  const sameElement = box.dataset.id === e.id;
  const trendHTML = sameElement && document.getElementById("trend") ? document.getElementById("trend").innerHTML : "";
  box.dataset.id = e.id;
  box.innerHTML = `
    <h2 class="el-name">${esc(e.label)}</h2>
    <span class="pill st-${e.status}">${STATUS_WORD[e.status] || e.status}</span>
    <p class="el-msg">${esc(e.message || (e.status === "ok" ? "Everything within limits." : ""))}</p>
    ${e.values.length ? `<dl class="el-values">${e.values.map(v =>
      `<dt>${esc(v.label)}</dt><dd>${esc(fmtValue(v.value))}${v.unit ? " " + esc(v.unit) : ""}</dd>`).join("")}</dl>` : ""}
    <div class="trend" id="trend">${trendHTML}</div>`;
  if (!sameElement || Date.now() - lastTrendAt > 10000) loadTrend(e.id);
}

function select(id) {
  selectedId = id;
  document.querySelectorAll("#sld .el").forEach(g => g.classList.toggle("selected", g.dataset.id === id));
  renderDetails();
}

function renderHeader() {
  document.title = `${snap.name} substation`;
  document.getElementById("name").textContent = snap.name;
  const kind = snap.type === "primary" ? "Primary substation" : "Transformer station";
  document.getElementById("rating").textContent = [kind, snap.rating, snap.region].filter(Boolean).join(", ");
  document.getElementById("status").innerHTML =
    `<span class="dot st-${snap.status}" aria-hidden="true"></span>
     <span class="text">${snap.status === "ok" ? "Working normally" : esc(snap.headline)}</span>`;
  document.getElementById("updated").textContent = snap.ts ? `Updated ${hhmmss(snap.ts)}` : "";
}

function setupDemo() {
  if (demoReady || !snap.demo || !snap.demo_actions || !Object.keys(snap.demo_actions).length) return;
  demoReady = true;
  document.getElementById("demo").hidden = false;
  document.getElementById("demo-action").innerHTML =
    Object.entries(snap.demo_actions).map(([k, v]) => `<option value="${esc(k)}">${esc(v)}</option>`).join("");
  const send = async action => {
    const msg = document.getElementById("demo-msg");
    try {
      const r = await fetch(`/api/demo/${encodeURIComponent(SID)}/${action}`, { method: "POST" });
      msg.textContent = r.ok ? (action === "clear" ? "Clearing. The diagram updates within a few seconds."
                                                    : "Simulating. The diagram updates within a few seconds.")
                             : "That didn't work. Check that the simulator is running.";
    } catch { msg.textContent = "Can't reach the portal server."; }
  };
  document.getElementById("demo-go").onclick = () => send(document.getElementById("demo-action").value);
  document.getElementById("demo-clear").onclick = () => send("clear");
}

// ------------------------------------------------------------------ refresh loop
async function refresh() {
  try {
    const [s, events] = await Promise.all([
      getJSON(`/api/substations/${encodeURIComponent(SID)}`),
      getJSON(`/api/substations/${encodeURIComponent(SID)}/events?limit=15`),
    ]);
    snap = s;
    if (selectedId === null && snap.elements.length) {
      const worstEl = [...snap.elements].sort((a, b) => SEVERITY[b.status] - SEVERITY[a.status])[0];
      if (SEVERITY[worstEl.status] >= 2) selectedId = worstEl.id;
    }
    renderHeader();
    drawDiagram();
    renderDetails();
    setupDemo();
    document.getElementById("events").innerHTML = events.length
      ? events.map(e => eventItem(e, false)).join("")
      : `<li><span></span><span class="hint">No changes recorded yet.</span></li>`;
  } catch (err) {
    document.getElementById("name").textContent = "Substation not found";
    document.getElementById("status").textContent = "Check the address, or go back to all substations.";
  }
}

const svgEl = document.getElementById("sld");
svgEl.addEventListener("click", ev => {
  const g = ev.target.closest(".el");
  if (g) select(g.dataset.id);
});
svgEl.addEventListener("keydown", ev => {
  const g = ev.target.closest(".el");
  if (g && (ev.key === "Enter" || ev.key === " ")) { ev.preventDefault(); select(g.dataset.id); }
});

refresh();
setInterval(refresh, 2000);
