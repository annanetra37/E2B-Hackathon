// On-screen SMS preview: shows the exact text of each alert / back-to-normal SMS while no real
// SMS provider is configured (SMS_PREVIEW=auto + SMS_PROVIDER=console), so the team can read
// what the phones will get. Turns itself off once SMS_PROVIDER is twilio / android_gateway.
(function () {
  const TITLE = { alert: "Alert SMS", recovery: "Back-to-normal SMS", lost: "Data-lost SMS",
                  outage: "Outage SMS", restored: "Restored SMS", nodata: "No-data SMS" };
  const MAX = 3;
  let box = null;

  function ensureBox() {
    if (box) return box;
    box = document.createElement("div");
    box.className = "sms-stack";
    box.setAttribute("aria-live", "polite");
    document.body.appendChild(box);
    return box;
  }

  function who(p) {
    if (!p.smsEnabled) return "SMS_ENABLED=false: nothing is sent";
    if (!p.targets) return `nobody signed up yet: would go to 0 ${p.channel === "email" ? "email addresses" : "phones"}`;
    const real = p.provider !== "console" && p.providerReady !== false;
    const unit = p.channel === "email" ? "email address" : "phone";
    const n = x => `${x} ${unit}${x === 1 ? "" : (unit === "phone" ? "s" : "es")}`;
    const skipped = p.sent < p.targets ? ` · ${p.targets - p.sent} skipped (cooldown or daily cap)` : "";
    return real ? `sent to ${p.sent} of ${n(p.targets)}${skipped}`
                : `would go to ${n(p.sent)} (provider not set up yet)${skipped}`;
  }

  window.showSmsPreview = function (p) {
    if (!p || !p.texts) return;
    const el = document.createElement("section");
    el.className = `sms-pop sms-${p.kind}`;
    const time = typeof clock === "function" ? clock(p.ts) : new Date(p.ts).toLocaleTimeString();
    const blocks = [["en", "English"], ["hy", "Հայերեն"]].filter(([k]) => p.texts[k]).map(([k, label]) =>
      `<div class="sms-lang"><span class="sms-tag">${label}</span><p lang="${k}">${esc(p.texts[k])}</p></div>`).join("");
    el.innerHTML = `
      <header><span class="sms-icon" aria-hidden="true">✉</span>
        <div><b>${(TITLE[p.kind] || "SMS").replace("SMS", p.channel === "email" ? "email" : "SMS")}</b> · ${time}<br><span class="sms-who">${esc(who(p))}</span></div>
        <button type="button" class="sms-close" aria-label="Close">×</button></header>
      ${blocks}
      <p class="sms-note">Preview on screen until ${p.channel === "email" ? "RESEND_API_KEY is set" : "a real SMS provider is configured"}.</p>`;
    el.querySelector(".sms-close").addEventListener("click", () => el.remove());
    const stack = ensureBox();
    stack.prepend(el);
    while (stack.children.length > MAX) stack.lastChild.remove();
  };
})();
