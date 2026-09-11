"""Self-contained HTML shell for the WebKit Host Info tab.

Loaded once via ``WebView.load_html``. Python pushes updates with
``evaluate_javascript("window.applyHostInfo(…)")`` /
``window.applyLive(…)``; the page asks for a refresh through
``window.webkit.messageHandlers.hostInfo.postMessage({type:"refresh"})``.

Kept WebKit-free so it is unit-testable headlessly.
"""

from __future__ import annotations

import json
from gettext import gettext as _


def build_host_info_html() -> str:
    """Return one self-contained Host Info document."""

    strings = {
        "overview": _("Overview"),
        "resources": _("Resources"),
        "storage": _("Storage"),
        "network": _("Network"),
        "traffic": _("Traffic"),
        "system": _("System"),
        "refresh": _("Refresh"),
        "gathering": _("Gathering host information…"),
        "load": _("Load average"),
        "cores": _("Per-core CPU"),
        "filesystems": _("Filesystems"),
        "interfaces": _("Interfaces"),
        "listening": _("Listening ports"),
        "sockets": _("Established sockets"),
        "processes": _("Top processes"),
        "temperatures": _("Temperatures"),
        "sessions": _("Login sessions"),
        "failed": _("Failed units"),
        "host_keys": _("SSH host keys"),
        "rx": _("Receive"),
        "tx": _("Transmit"),
        "read_only": _("read-only"),
        "empty": _("Nothing to show"),
        "updated": _("Updated"),
    }
    strings_json = json.dumps(strings, ensure_ascii=False)
    # CSS and JS are plain strings (no f-string) so braces stay literal.
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<script>window.HOST_INFO_I18N = {strings_json};</script>"
        "<style>"
        + _CSS
        + "</style></head><body>"
        "<div id='app'>"
        "<header class='topbar'>"
        "<div class='titles'>"
        "<div id='title' class='title'></div>"
        "<div id='subtitle' class='subtitle'></div>"
        "</div>"
        "<div class='actions'>"
        "<span id='age' class='age'></span>"
        "<button id='refresh' type='button'></button>"
        "</div>"
        "</header>"
        "<nav id='tabs' class='tabs' hidden></nav>"
        "<main id='main'></main>"
        "</div>"
        "<script>"
        + _JS
        + "</script></body></html>"
    )


_CSS = """
:root {
  color-scheme: light dark;
  --bg: #f6f5f4;
  --panel: #ffffff;
  --text: #241f31;
  --muted: #77767b;
  --border: rgba(0,0,0,.08);
  --ok: #2ec27e;
  --careful: #3584e4;
  --warn: #e5a50a;
  --critical: #e01b24;
  --unknown: #9a9996;
  --bar-track: rgba(0,0,0,.08);
  --shadow: 0 1px 2px rgba(0,0,0,.06);
  font-family: "Cantarell", "Segoe UI", system-ui, sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #242424;
    --panel: #303030;
    --text: #eeeeec;
    --muted: #c0bfbc;
    --border: rgba(255,255,255,.1);
    --bar-track: rgba(255,255,255,.12);
    --shadow: 0 1px 2px rgba(0,0,0,.35);
  }
}
* { box-sizing: border-box; }
html, body {
  margin: 0; height: 100%;
  background: var(--bg); color: var(--text);
}
#app {
  display: flex; flex-direction: column; height: 100%;
}
.topbar {
  display: flex; align-items: center; gap: 16px;
  padding: 14px 20px 10px; border-bottom: 1px solid var(--border);
  background: color-mix(in srgb, var(--panel) 92%, transparent);
  position: sticky; top: 0; z-index: 2;
}
.titles { min-width: 0; flex: 1; }
.title { font-size: 1.05rem; font-weight: 700; }
.subtitle {
  color: var(--muted); font-size: .85rem;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.actions { display: flex; align-items: center; gap: 10px; flex-shrink: 0; }
.age { color: var(--muted); font-size: .78rem; }
button {
  appearance: none; border: 1px solid var(--border); background: var(--panel);
  color: var(--text); border-radius: 999px; padding: 6px 14px;
  font: inherit; cursor: pointer; box-shadow: var(--shadow);
}
button:hover { filter: brightness(1.05); }
button:disabled { opacity: .55; cursor: default; }
.tabs {
  display: flex; gap: 4px; padding: 10px 16px 0; overflow-x: auto;
}
.tabs button {
  border-radius: 10px; padding: 7px 12px; box-shadow: none;
  background: transparent; border-color: transparent; color: var(--muted);
}
.tabs button.active {
  background: var(--panel); color: var(--text); border-color: var(--border);
  box-shadow: var(--shadow);
}
#main {
  flex: 1; overflow: auto; padding: 16px 20px 28px;
}
.status {
  min-height: 50vh; display: grid; place-items: center; text-align: center;
  color: var(--muted); gap: 12px;
}
.spinner {
  width: 28px; height: 28px; border-radius: 50%;
  border: 3px solid var(--border); border-top-color: var(--careful);
  animation: spin 0.8s linear infinite; margin: 0 auto 12px;
}
@keyframes spin { to { transform: rotate(360deg); } }
.grid { display: grid; gap: 12px; }
.gauges { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.cards3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.card {
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 14px; padding: 14px 16px; box-shadow: var(--shadow);
}
.section-title {
  margin: 18px 0 8px; font-size: .78rem; letter-spacing: .04em;
  text-transform: uppercase; color: var(--muted); font-weight: 700;
}
.gauge-title { color: var(--muted); font-size: .85rem; margin-bottom: 8px; }
.gauge-ring {
  --pct: 0;
  /* --tone comes from .sev-* on this element (or a parent). */
  width: 96px; height: 96px; margin: 0 auto 10px; border-radius: 50%;
  background:
    radial-gradient(closest-side, var(--panel) 72%, transparent 73% 100%),
    conic-gradient(var(--tone, var(--unknown)) calc(var(--pct) * 1%), var(--bar-track) 0);
  display: grid; place-items: center; font-weight: 700; font-size: 1.1rem;
}
.gauge-detail, .gauge-sub { text-align: center; font-size: .85rem; }
.gauge-sub { color: var(--muted); margin-top: 2px; }
.kv { display: grid; grid-template-columns: minmax(120px, 180px) 1fr; gap: 8px 16px; }
.kv .label { color: var(--muted); }
.mono, .mono * { font-family: ui-monospace, "Source Code Pro", monospace; }
.bar {
  --pct: 0; --tone: var(--unknown);
  height: 8px; border-radius: 999px; background: var(--bar-track); overflow: hidden;
}
.bar > i {
  display: block; height: 100%; width: calc(var(--pct) * 1%);
  background: var(--tone); border-radius: inherit;
}
.row {
  display: flex; justify-content: space-between; gap: 12px; align-items: baseline;
  margin-bottom: 6px;
}
.muted { color: var(--muted); }
.small { font-size: .82rem; }
.badge {
  display: inline-block; padding: 1px 7px; border-radius: 999px;
  background: var(--bar-track); color: var(--muted); font-size: .75rem;
}
.table { width: 100%; border-collapse: collapse; font-size: .9rem; }
.table th, .table td {
  text-align: left; padding: 8px 6px; border-bottom: 1px solid var(--border);
  vertical-align: top;
}
.table th { color: var(--muted); font-weight: 600; font-size: .78rem; }
/* Top-process command lines (and similar argv soups) must clip. */
.table-clip { table-layout: fixed; }
.table-clip td:first-child {
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.sev-ok { --tone: var(--ok); }
.sev-careful { --tone: var(--careful); }
.sev-warn { --tone: var(--warn); }
.sev-critical { --tone: var(--critical); }
.sev-unknown { --tone: var(--unknown); }
@media (max-width: 900px) {
  .gauges, .cards3 { grid-template-columns: 1fr; }
}
"""


_JS = """
(function () {
  const S = window.HOST_INFO_I18N || {};
  const state = { tab: "overview", data: null, ageSec: null, ageTimer: null };
  const main = document.getElementById("main");
  const tabs = document.getElementById("tabs");
  const titleEl = document.getElementById("title");
  const subtitleEl = document.getElementById("subtitle");
  const ageEl = document.getElementById("age");
  const refreshBtn = document.getElementById("refresh");

  refreshBtn.textContent = S.refresh || "Refresh";
  refreshBtn.addEventListener("click", function () {
    refreshBtn.disabled = true;
    post({ type: "refresh" });
  });

  const TAB_DEFS = [
    ["overview", S.overview],
    ["resources", S.resources],
    ["storage", S.storage],
    ["network", S.network],
    ["traffic", S.traffic],
    ["system", S.system],
  ];

  function post(msg) {
    try {
      if (window.webkit && window.webkit.messageHandlers &&
          window.webkit.messageHandlers.hostInfo) {
        // Match the PyXterm bridge: stringify so Python always sees a JSON
        // string value (WebKit bindings differ on raw object delivery).
        window.webkit.messageHandlers.hostInfo.postMessage(JSON.stringify(msg));
      }
    } catch (e) {}
  }

  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function sevClass(sev) {
    return "sev-" + (sev || "unknown");
  }

  function pct(fraction) {
    if (fraction == null || isNaN(fraction)) return 0;
    return Math.max(0, Math.min(100, Math.round(fraction * 100)));
  }

  function gaugeHtml(g) {
    const sev = sevClass(g.severity);
    return (
      '<div class="card ' + sev + '">' +
        '<div class="gauge-title">' + esc(g.title) + '</div>' +
        '<div class="gauge-ring ' + sev + '" style="--pct:' + pct(g.fraction) + '">' +
          esc(g.percent || "—") +
        '</div>' +
        '<div class="gauge-detail">' + esc(g.detail || "") + '</div>' +
        '<div class="gauge-sub">' + esc(g.subtitle || "") + '</div>' +
      '</div>'
    );
  }

  function barHtml(fraction, severity) {
    return (
      '<div class="bar ' + sevClass(severity) + '" style="--pct:' +
      pct(fraction) + '"><i></i></div>'
    );
  }

  function kvHtml(rows) {
    return (
      '<div class="card"><div class="kv">' +
      rows.map(function (row) {
        return (
          '<div class="label">' + esc(row.label) + '</div>' +
          '<div class="' + (row.mono ? "mono" : "") + '">' + esc(row.value) + '</div>'
        );
      }).join("") +
      '</div></div>'
    );
  }

  function emptyHtml() {
    return '<div class="card muted">' + esc(S.empty || "Nothing to show") + '</div>';
  }

  function section(title, body) {
    return '<div class="section-title">' + esc(title) + '</div>' + body;
  }

  function renderStatus(data) {
    tabs.hidden = true;
    const busy = data.status === "busy";
    main.innerHTML =
      '<div class="status">' +
        (busy ? '<div class="spinner"></div>' : '') +
        '<div>' + esc(data.message || "") + '</div>' +
      '</div>';
    refreshBtn.disabled = busy;
  }

  function renderOverview(data) {
    return (
      '<div class="grid gauges">' +
        (data.gauges || []).map(gaugeHtml).join("") +
      '</div>' +
      '<div style="height:12px"></div>' +
      kvHtml(data.overview_rows || [])
    );
  }

  function renderResources(data) {
    const loads = (data.load_cards || []).map(function (card) {
      return (
        '<div class="card">' +
          '<div class="row"><span class="muted small">' + esc(card.title) +
          '</span><span class="mono">' + esc(card.value) + '</span></div>' +
          barHtml(card.fraction, card.severity) +
        '</div>'
      );
    }).join("");
    const cores = (data.cores || []).map(function (core) {
      return (
        '<div class="card">' +
          '<div class="row"><span class="mono">' + esc(core.name) +
          '</span><span class="mono">' + esc(core.percent) + '</span></div>' +
          barHtml(core.fraction, core.severity) +
        '</div>'
      );
    }).join("");
    return (
      section(S.load || "Load average",
        loads ? '<div class="grid cards3">' + loads + '</div>' : emptyHtml()) +
      section(S.cores || "Per-core CPU",
        cores ? '<div class="grid cards3">' + cores + '</div>' : emptyHtml())
    );
  }

  function renderStorage(data) {
    const items = (data.filesystems || []).map(function (fs) {
      return (
        '<div class="card">' +
          '<div class="row"><strong class="mono">' + esc(fs.mount) +
          '</strong><span class="mono">' + esc(fs.percent) + '</span></div>' +
          '<div class="muted small">' + esc(fs.detail) +
          (fs.read_only ? ' · <span class="badge">' + esc(S.read_only) + '</span>' : '') +
          (fs.inode_note ? ' · ' + esc(fs.inode_note) : '') +
          '</div>' +
          '<div class="muted small" style="margin:6px 0">' +
            esc(fs.fstype) + ' · ' + esc(fs.device) +
          '</div>' +
          barHtml(fs.fraction, fs.severity) +
        '</div>'
      );
    }).join("");
    return section(S.filesystems || "Filesystems", items || emptyHtml());
  }

  function renderNetwork(data) {
    const rows = (data.interfaces || []).map(function (iface) {
      return (
        '<tr>' +
          '<td class="mono">' + esc(iface.name) + '</td>' +
          '<td>' + esc(iface.state) + ' / ' + esc(iface.kind) + '</td>' +
          '<td class="mono">' + esc(iface.addresses) + '</td>' +
          '<td class="mono">' + esc(iface.rx_rate) + '</td>' +
          '<td class="mono">' + esc(iface.tx_rate) + '</td>' +
        '</tr>'
      );
    }).join("");
    const table = rows
      ? '<div class="card"><table class="table"><thead><tr>' +
          '<th>Name</th><th>State</th><th>Addresses</th><th>' +
          esc(S.rx) + '</th><th>' + esc(S.tx) + '</th>' +
        '</tr></thead><tbody>' + rows + '</tbody></table></div>'
      : emptyHtml();
    return section(S.interfaces || "Interfaces", table);
  }

  function renderTraffic(data) {
    const listenRows = (data.listening || []).map(function (p) {
      return '<tr><td class="mono">' + esc(p.address) +
        '</td><td class="mono">' + esc(p.process) + '</td></tr>';
    }).join("");
    const sockRows = (data.sockets || []).map(function (s) {
      return '<tr><td>' + esc(s.direction) + '</td><td class="mono">' +
        esc(s.local) + '</td><td class="mono">' + esc(s.remote) +
        '</td><td class="mono">' + esc(s.process) + '</td></tr>';
    }).join("");
    return (
      section(S.listening || "Listening ports",
        listenRows
          ? '<div class="card"><table class="table"><thead><tr><th>Port</th><th>Process</th></tr></thead><tbody>' +
            listenRows + '</tbody></table></div>'
          : emptyHtml()) +
      section(S.sockets || "Established sockets",
        sockRows
          ? '<div class="card"><table class="table"><thead><tr><th>Dir</th><th>Local</th><th>Remote</th><th>Process</th></tr></thead><tbody>' +
            sockRows + '</tbody></table></div>'
          : emptyHtml())
    );
  }

  function renderSystem(data) {
    const procRows = (data.processes || []).map(function (p) {
      return '<tr><td class="mono" title="' + esc(p.name) + '">' + esc(p.name) +
        '</td><td class="mono">' + esc(p.cpu) +
        '</td><td class="mono">' + esc(p.mem) + '</td></tr>';
    }).join("");
    const tempCards = (data.temperatures || []).map(function (t) {
      return '<div class="card ' + sevClass(t.severity) + '"><div class="row">' +
        '<span>' + esc(t.label) + '</span><strong>' + esc(t.value) +
        '</strong></div></div>';
    }).join("");
    const sessionRows = (data.sessions || []).map(function (s) {
      return '<tr><td>' + esc(s.user) + '</td><td class="mono">' + esc(s.tty) +
        '</td><td class="mono">' + esc(s.from) +
        '</td><td class="mono">' + esc(s.since) + '</td></tr>';
    }).join("");
    const failed = (data.failed_units || []).map(function (u) {
      return '<div class="card"><strong class="mono">' + esc(u.unit) +
        '</strong><div class="muted small">' + esc(u.state) + '</div></div>';
    }).join("");
    const keys = (data.host_keys || []).map(function (k) {
      return '<div class="card mono small">' + esc(k.algorithm) +
        (k.bits != null ? ' (' + esc(k.bits) + ')' : '') +
        '<div>' + esc(k.fingerprint) + '</div></div>';
    }).join("");
    return (
      kvHtml(data.system_rows || []) +
      section(S.processes || "Top processes",
        procRows
          ? '<div class="card"><table class="table table-clip"><thead><tr><th>Command</th><th>CPU</th><th>Mem</th></tr></thead><tbody>' +
            procRows + '</tbody></table></div>'
          : emptyHtml()) +
      section(S.temperatures || "Temperatures",
        tempCards ? '<div class="grid cards3">' + tempCards + '</div>' : emptyHtml()) +
      section(S.sessions || "Login sessions",
        sessionRows
          ? '<div class="card"><table class="table"><thead><tr><th>User</th><th>TTY</th><th>From</th><th>Since</th></tr></thead><tbody>' +
            sessionRows + '</tbody></table></div>'
          : emptyHtml()) +
      section(S.failed || "Failed units", failed || emptyHtml()) +
      section(S.host_keys || "SSH host keys", keys || emptyHtml())
    );
  }

  function renderReady() {
    const data = state.data;
    if (!data) return;
    tabs.hidden = false;
    titleEl.textContent = data.title || "";
    subtitleEl.textContent = data.subtitle || "";
    refreshBtn.disabled = false;
    let html = "";
    if (state.tab === "overview") html = renderOverview(data);
    else if (state.tab === "resources") html = renderResources(data);
    else if (state.tab === "storage") html = renderStorage(data);
    else if (state.tab === "network") html = renderNetwork(data);
    else if (state.tab === "traffic") html = renderTraffic(data);
    else html = renderSystem(data);
    main.innerHTML = html;
  }

  function buildTabs() {
    tabs.innerHTML = "";
    TAB_DEFS.forEach(function (pair) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = pair[1] || pair[0];
      btn.dataset.tab = pair[0];
      if (pair[0] === state.tab) btn.classList.add("active");
      btn.addEventListener("click", function () {
        state.tab = pair[0];
        Array.prototype.forEach.call(tabs.querySelectorAll("button"), function (b) {
          b.classList.toggle("active", b.dataset.tab === state.tab);
        });
        renderReady();
      });
      tabs.appendChild(btn);
    });
  }

  function tickAge() {
    if (state.ageSec == null) {
      ageEl.textContent = "";
      return;
    }
    state.ageSec += 1;
    const n = state.ageSec;
    ageEl.textContent = (S.updated || "Updated") + " " + n + "s";
  }

  function resetAge() {
    state.ageSec = 0;
    ageEl.textContent = (S.updated || "Updated") + " 0s";
    if (state.ageTimer) clearInterval(state.ageTimer);
    state.ageTimer = setInterval(tickAge, 1000);
  }

  window.applyHostInfo = function (payload) {
    try {
      const data = typeof payload === "string" ? JSON.parse(payload) : payload;
      titleEl.textContent = data.title || "";
      subtitleEl.textContent = data.subtitle || "";
      if (data.status === "busy" || data.status === "error") {
        state.data = data;
        renderStatus(data);
        post({ type: "applied", fn: "applyHostInfo", status: data.status });
        return;
      }
      state.data = data;
      buildTabs();
      resetAge();
      renderReady();
      post({ type: "applied", fn: "applyHostInfo", status: data.status || "ready" });
    } catch (err) {
      console.error("applyHostInfo failed", err);
      post({
        type: "js-error",
        message: String(err && err.message ? err.message : err),
        fn: "applyHostInfo"
      });
    }
  };

  window.applyLive = function (payload) {
    try {
      const live = typeof payload === "string" ? JSON.parse(payload) : payload;
      if (!state.data || state.data.status !== "ready") {
        post({
          type: "applied",
          fn: "applyLive",
          status: "skipped",
          reason: !state.data ? "no-data" : state.data.status
        });
        return;
      }
      if (live.cpu && state.data.gauges && state.data.gauges[0]) {
        state.data.gauges[0] = live.cpu;
      }
      if (live.memory && state.data.gauges && state.data.gauges[1]) {
        state.data.gauges[1] = live.memory;
      }
      if (live.cores) state.data.cores = live.cores;
      if (live.rates && state.data.interfaces) {
        state.data.interfaces.forEach(function (iface) {
          const rate = live.rates[iface.name];
          if (rate) {
            iface.rx_rate = rate.rx_rate;
            iface.tx_rate = rate.tx_rate;
          }
        });
      }
      resetAge();
      if (state.tab === "overview" || state.tab === "resources" || state.tab === "network") {
        renderReady();
      }
      post({ type: "applied", fn: "applyLive", status: "ready" });
    } catch (err) {
      console.error("applyLive failed", err);
      post({
        type: "js-error",
        message: String(err && err.message ? err.message : err),
        fn: "applyLive"
      });
    }
  };

  window.applyHostInfo({
    title: "Host Info",
    subtitle: "",
    status: "busy",
    message: S.gathering || "Gathering host information…"
  });
  post({ type: "ready" });
})();
"""
