// Small helpers shared by both pages.
const STATUS_WORD = {
  ok: "Working", warning: "Warning", fault: "Not working",
  offline: "No data", unmonitored: "Not monitored",
};
const SEVERITY = { fault: 3, offline: 3, warning: 2, ok: 1, unmonitored: 0 };

async function getJSON(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// server timestamps are UTC "YYYY-MM-DD HH:MM:SS"
function toDate(ts) { return new Date(ts.replace(" ", "T") + "Z"); }
function hhmm(ts) { return toDate(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }
function hhmmss(ts) { return toDate(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }); }

function fmtValue(v) {
  if (typeof v === "number") return v.toLocaleString(undefined, { maximumFractionDigits: 2 });
  return v ?? "–";
}

function eventItem(e, withSubstation) {
  const where = withSubstation ? `<b>${esc(e.substation_name)}</b>, ` : "";
  return `<li class="st-${esc(e.status)}"><span class="ev-time">${hhmm(e.ts)}</span>
    <span class="ev-text"><span class="mark"></span>${where}${esc(e.label)}: ${esc(e.message)}</span></li>`;
}

// Alert channel (ALERT_CHANNEL on the server): label the sign-up links "Get SMS alerts" or "Get email alerts".
const ALERT_LABELS = {
  sms: { nav: "Get SMS alerts", qr: "Scan to get the SMS alert", button: "Get the SMS alert on your phone · Ստացեք SMS ահազանգ",
         simhint: "Want the SMS on this phone?", test: "Send test SMS" },
  email: { nav: "Get email alerts", qr: "Scan to get email alerts", button: "Get email alerts · Ստացեք ահազանգ էլ. փոստով",
           simhint: "Want the alert emails?", test: "Send test email" },
};
function applyAlertChannel(channel) {
  const c = channel === "email" ? "email" : "sms";
  window.ALERT_CHANNEL = c;
  document.querySelectorAll("[data-alert-label]").forEach(el => {
    const t = ALERT_LABELS[c][el.dataset.alertLabel];
    if (t) el.textContent = t;
  });
  const tp = document.getElementById("test-phone");
  if (tp && c === "email" && tp.type !== "email") {
    tp.type = "email"; tp.value = ""; tp.placeholder = "name@example.com"; tp.setAttribute("aria-label", "Email for a test message");
  }
}
fetch("/api/demo/channel", { cache: "no-store" }).then(r => r.ok ? r.json() : null)
  .then(d => d && applyAlertChannel(d.channel)).catch(() => {});
