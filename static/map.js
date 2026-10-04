// Map page: Armenia, every substation as a marker, list + latest changes on the left.
const map = L.map("map", { zoomSnap: 0.25 }).setView([40.1, 45.0], 8);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 18,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
}).addTo(map);

// legend
const legend = L.control({ position: "bottomleft" });
legend.onAdd = () => {
  const d = L.DomUtil.create("div", "map-legend");
  d.innerHTML = `
    <div><span class="lg-shape primary"></span>Primary substation, 110/10 kV</div>
    <div><span class="lg-shape distribution"></span>Transformer station, to 0.4 kV</div>
    <div class="lg-colors"><span class="st-ok"><i></i>Working</span><span class="st-warning"><i></i>Warning</span><span class="st-fault"><i></i>Problem</span><span class="st-offline"><i></i>No data</span></div>`;
  return d;
};
legend.addTo(map);

const markers = {};
const narrow = window.matchMedia("(max-width: 860px)").matches;   // phones: no permanent labels
let fitted = false;

function icon(s) {
  return L.divIcon({
    className: "",
    html: `<div class="ss-marker ${s.type} st-${s.status}"><span class="ring"></span><span class="core"></span></div>`,
    iconSize: [26, 26], iconAnchor: [13, 13],
  });
}

function openStation(id) { window.location.href = `/substation/${encodeURIComponent(id)}`; }

function renderList(subs) {
  const sorted = [...subs].sort((a, b) =>
    (SEVERITY[b.status] - SEVERITY[a.status]) || a.name.localeCompare(b.name));
  document.getElementById("list").innerHTML = sorted.map(s => `
    <li class="st-${esc(s.status)}">
      <a href="/substation/${encodeURIComponent(s.id)}">
        <span class="dot shape-${esc(s.type)}" aria-hidden="true"></span>
        <span class="sub-name">${esc(s.name)}<span class="sub-region">${esc(s.region)}</span></span>
        <span class="sub-headline"><span class="visually-hidden">${STATUS_WORD[s.status] || s.status}. </span>${esc(s.headline)}</span>
      </a>
    </li>`).join("");

  const bad = subs.filter(s => s.status === "fault" || s.status === "offline").length;
  const warn = subs.filter(s => s.status === "warning").length;
  const good = subs.length - bad - warn;
  let text = `<strong>${good} of ${subs.length}</strong> working normally.`;
  if (bad) text += ` <strong class="t-fault">${bad} with a problem.</strong>`;
  if (warn) text += ` ${warn} with a warning.`;
  document.getElementById("summary").innerHTML = text;
}

function renderMarkers(subs) {
  for (const s of subs) {
    let m = markers[s.id];
    if (!m) {
      m = markers[s.id] = L.marker([s.lat, s.lon], { icon: icon(s), keyboard: true, title: s.name, riseOnHover: true })
        .on("click", () => openStation(s.id)).addTo(map);
      m._status = s.status;
    } else if (m._status !== s.status) {
      m.setIcon(icon(s));
      m._status = s.status;
    }
    const bad = (s.status === "fault" || s.status === "offline") && !narrow;
    const html = `<b>${esc(s.name)}</b>${s.status !== "ok" ? `<br><span class="tt-msg">${esc(s.headline)}</span>` : ""}`;
    if (!m.getTooltip() || m._permanent !== bad) {
      m.unbindTooltip();
      m.bindTooltip(html, { permanent: bad, direction: "right", offset: [12, 0], className: `ss-label st-${s.status}` });
      m._permanent = bad;
    } else {
      m.setTooltipContent(html);
    }
    if (m.getTooltip()) m.getTooltip().options.className = `ss-label st-${s.status}`;
  }
  if (!fitted && subs.length) {
    map.fitBounds(L.latLngBounds(subs.map(s => [s.lat, s.lon])),
                  narrow ? { padding: [24, 24] }
                         : { paddingTopLeft: [40, 40], paddingBottomRight: [300, 40] });   // room for labels
    fitted = true;
  }
}

async function refresh() {
  try {
    const [data, events] = await Promise.all([getJSON("/api/substations"), getJSON("/api/events?limit=12")]);
    renderList(data.substations);
    renderMarkers(data.substations);
    document.getElementById("feed").innerHTML = events.length
      ? events.map(e => eventItem(e, true)).join("")
      : `<li><span></span><span class="hint">No changes yet.</span></li>`;
  } catch (err) {
    document.getElementById("summary").textContent = "Can't reach the portal server. Retrying…";
  }
}
refresh();
setInterval(refresh, 3000);
