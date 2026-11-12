// On-screen SMS preview: shows the exact text of each alert / back-to-normal SMS while no real
// SMS provider is configured (SMS_PREVIEW=auto + SMS_PROVIDER=console), so the team can read
// what the phones will get. Turns itself off once SMS_PROVIDER is twilio / android_gateway.
(function () {
  const TITLE = { alert: "Alert SMS", recovery: "Back-to-normal SMS", lost: "Data-lost SMS" };
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
    if (!p.targets) return "nobody signed up yet: would go to 0 phones";
    const real = p.provider !== "console";
    return real ? `sent to ${p.sent} of ${p.targets} phone${p.targets === 1 ? "" : "s"}`
                : `would go to ${p.targets} phone${p.targets === 1 ? "" : "s"} (provider not set up yet)`;
  }

  window.showSmsPreview = function (p) {
    if (!p || !p.texts) return;
    const el = document.createElement("section");
    el.className = `sms-pop sms-${p.kind}`;
    const time = new Date(p.ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
    const blocks = [["en", "English"], ["hy", "Հայերեն"]].filter(([k]) => p.texts[k]).map(([k, label]) =>
      `<div class="sms-lang"><span class="sms-tag">${label}</span><p lang="${k}">${esc(p.texts[k])}</p></div>`).join("");
    el.innerHTML = `
      <header><span class="sms-icon" aria-hidden="true">✉</span>
        <div><b>${TITLE[p.kind] || "SMS"}</b> · ${time}<br><span class="sms-who">${esc(who(p))}</span></div>
        <button type="button" class="sms-close" aria-label="Close">×</button></header>
      ${blocks}
      <p class="sms-note">Preview on screen until a real SMS provider is configured.</p>`;
    el.querySelector(".sms-close").addEventListener("click", () => el.remove());
    const stack = ensureBox();
    stack.prepend(el);
    while (stack.children.length > MAX) stack.lastChild.remove();
  };
})();
