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
// Every time on every page is shown in the station's time zone (server TZ, default Asia/Yerevan),
// not in the viewer's own browser time zone.
window.APP_TZ = window.APP_TZ || "Asia/Yerevan";
function clock(date, seconds = true) {
  const opts = { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: window.APP_TZ };
  if (seconds) opts.second = "2-digit";
  try { return new Date(date).toLocaleTimeString("en-GB", opts); }
  catch (e) { delete opts.timeZone; return new Date(date).toLocaleTimeString("en-GB", opts); }
}
function hhmm(ts) { return clock(toDate(ts), false); }
function hhmmss(ts) { return clock(toDate(ts)); }

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
function applyAlertChannel(channel, tz) {
  if (tz) window.APP_TZ = tz;
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
  .then(d => d && applyAlertChannel(d.channel, d.timeZone)).catch(() => {});
