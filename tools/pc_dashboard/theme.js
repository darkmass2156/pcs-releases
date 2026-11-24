// PCS shared theme (2026-11-22): palette + light / dark from the Settings page, kept in this browser (localStorage).
//   pcs-theme   "auto" | "light" | "dark"   (same key the Dashboard and Analyzer already used)
//   pcs-palette "glacier" (default) | "aurora" | "lime" | "ember" | "harbor" | "paprika" | "scarlet" | "cinnabar" | "sandstone"
// Sets <html data-palette data-mode data-theme> before first paint and follows the OS / other tabs live.
(function () {
  const PALETTES = ["glacier", "aurora", "lime", "ember", "harbor", "paprika", "scarlet", "cinnabar", "sandstone"];
  const get = (k, d) => { try { return localStorage.getItem(k) || d; } catch (e) { return d; } };
  const mq = matchMedia("(prefers-color-scheme: dark)");
  function apply() {
    const theme = get("pcs-theme", "auto");
    let pal = get("pcs-palette", "glacier");
    if (!PALETTES.includes(pal)) pal = "glacier";
    const mode = theme === "auto" ? (mq.matches ? "dark" : "light") : theme;
    const r = document.documentElement;
    r.setAttribute("data-palette", pal);
    r.setAttribute("data-mode", mode);
    if (theme === "auto") r.removeAttribute("data-theme"); else r.setAttribute("data-theme", theme);
    window.dispatchEvent(new Event("pcs-theme"));
    tellApp();
  }
  // inside PCS.exe: hand the window title bar the page's mode and colours (WebView2 message)
  function tellApp() {
    const host = window.chrome && window.chrome.webview;
    if (!host || !document.body) return;
    const cs = getComputedStyle(document.documentElement);
    host.postMessage({ cmd: "theme", mode: document.documentElement.getAttribute("data-mode"),
                       bg: cs.getPropertyValue("--bg").trim(), fg: cs.getPropertyValue("--label").trim() });
  }
  addEventListener("load", tellApp);
  window.pcsTheme = {
    PALETTES, apply,
    set(theme, palette) {
      try {
        if (theme) localStorage.setItem("pcs-theme", theme);
        if (palette) localStorage.setItem("pcs-palette", palette);
      } catch (e) {}
      apply();
    },
    get theme() { return get("pcs-theme", "auto"); },
    get palette() { return get("pcs-palette", "glacier"); },
  };
  apply();
  mq.addEventListener("change", apply);
  addEventListener("storage", e => { if (e.key === "pcs-theme" || e.key === "pcs-palette") apply(); });
  const I = {
    home: '<path d="M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z"/>',
    dash: '<rect x="3" y="3" width="7.5" height="9" rx="2"/><rect x="13.5" y="3" width="7.5" height="5.5" rx="2"/><rect x="3" y="15" width="7.5" height="6" rx="2"/><rect x="13.5" y="11.5" width="7.5" height="9.5" rx="2"/>',
    full: '<path d="m12 3 9 5-9 5-9-5z"/><path d="m3 13 9 5 9-5"/>',
    analyzer: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
    fans: '<circle cx="12" cy="12" r="1.8"/><path d="M12 10.2C11 6.5 12 3.5 14.6 3.6c2.4.1 2.6 3.4-.9 6.9M13.8 12c3.7-1 6.7 0 6.6 2.6-.1 2.4-3.4 2.6-6.9-.9M12 13.8c1 3.7 0 6.7-2.6 6.6-2.4-.1-2.6-3.4.9-6.9M10.2 12c-3.7 1-6.7 0-6.6-2.6.1-2.4 3.4-2.6 6.9.9"/>',
    settings: '<circle cx="12" cy="12" r="3.2"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    moon: '<path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/>',
    auto: '<circle cx="12" cy="12" r="9"/><path d="M12 3v18" /><path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor"/>',
  };
  const svg = (p, sw = 1.8) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="${sw}" stroke-linecap="round" stroke-linejoin="round">${p}</svg>`;
  window.pcsIcon = (k, sw) => svg(I[k] || "", sw);
  window.pcsMark = '<svg viewBox="0 0 24 24" fill="none"><path d="M12 2.5c3.6 4.3 6.5 8 6.5 11.6A6.5 6.5 0 0 1 5.5 14C5.5 10.5 8.4 6.8 12 2.5z" fill="currentColor"/><path d="M9 14.5a3 3 0 0 0 3 3" stroke="rgba(0,0,0,.25)" stroke-width="1.6" stroke-linecap="round"/></svg>';
  // icon rail: <aside class="rail" data-page="home"></aside>
  addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("aside.rail[data-page]").forEach(r => {
      const items = [["home", "/home", "Home"], ["dash", "/", "Dashboard"], ["full", "/full", "Details"],
                     ["analyzer", "/analyzer", "Analyzer"], ["fans", "/fans", "Fans"], ["settings", "/settings", "Settings"]];
      r.innerHTML = `<div class="mark" title="PCS">${window.pcsMark}</div>` +
        items.map(([k, h, t]) => `<a href="${h}"${k === r.dataset.page ? ' class="on"' : ""}>${svg(I[k])}<span>${t}</span></a>`).join("") +
        `<div class="grow"></div><button class="mode-t" id="railTheme" title="Appearance"></button><div class="live" id="railLive" title="Controller"></div>`;
      const btn = r.querySelector("#railTheme");
      const paint = () => { const t = window.pcsTheme.theme; btn.innerHTML = svg(I[t === "auto" ? "auto" : t === "light" ? "sun" : "moon"]);
        btn.title = "Appearance: " + (t === "auto" ? "System" : t[0].toUpperCase() + t.slice(1)); };
      btn.onclick = () => { const o = ["auto", "light", "dark"]; window.pcsTheme.set(o[(o.indexOf(window.pcsTheme.theme) + 1) % 3]); paint(); };
      paint();
    });
  });
  // live dot for pages that don't report it themselves
  addEventListener("DOMContentLoaded", () => {
    if (!document.querySelector("aside.rail") || document.body.classList.contains("dash")) return;
    const tick = () => fetch("/status", { cache: "no-store" }).then(r => r.json()).then(s => window.pcsLive(s.connected))
      .catch(() => window.pcsLive(false));
    tick(); setInterval(tick, 5000);
  });
  window.pcsLive = ok => { const d = document.getElementById("railLive"); if (d) { d.className = "live " + (ok ? "ok" : "bad");
    d.title = ok ? "Controller connected" : "Controller not connected"; } };
  // shared top navigation: <nav class="pnav" data-page="home"></nav>
  addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("nav.pnav[data-page]").forEach(n => {
      const items = [["home", "/home", "Home"], ["dash", "/", "Dashboard"], ["full", "/full", "Full"],
                     ["analyzer", "/analyzer", "Analyzer"], ["fans", "/fans", "Fans"], ["settings", "/settings", "Settings"]];
      n.innerHTML = items.map(([k, h, t]) => `<a href="${h}"${k === n.dataset.page ? ' class="on"' : ""}>${t}</a>`).join("");
    });
  });
})();
