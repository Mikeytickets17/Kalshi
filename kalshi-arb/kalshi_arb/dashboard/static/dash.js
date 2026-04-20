// Dashboard client. Step 4: per-tab wiring + drawer + filters + charts
// + SSE in-place updates.
//
//   * Connects to /events/stream via EventSource. Falls back to
//     /events/poll every 5 s after 3 consecutive errors.
//   * Each tab's template declares a <script type="application/json"
//     id="sse-handlers"> describing which entity_types trigger what
//     refresh behavior (no-op, refresh current tab, refetch data
//     endpoint + patch DOM).
//   * Drawer: any element with class .row-clickable and a
//     data-opp-id opens the right-side drawer, fetches the tab's
//     /detail JSON, and renders a per-tab body.
//   * Filter forms (#opps-filter-form, #trades-filter-form,
//     #pnl-time-form) reload the page with the form's querystring
//     so the URL remains bookmarkable and SSR matches SPA state.
//   * CSV links mirror the current filter querystring so the
//     downloaded file matches the on-screen table.
//   * Charts: initialized on DOMContentLoaded from the embedded
//     <script id="tab-data"> JSON blob.

(function () {
  "use strict";

  // ---------------- helpers ----------------

  const PATH = window.location.pathname; // e.g. '/overview'
  const STATUS_EL_ID = "sse-status";
  const FALLBACK_POLL_MS = 5000;
  const MAX_CONSECUTIVE_ERRORS_BEFORE_FALLBACK = 3;

  const statusEl = () => document.getElementById(STATUS_EL_ID);
  const setStatus = (text, cls) => {
    const el = statusEl();
    if (!el) return;
    el.textContent = text;
    el.className = cls || "";
  };

  function readJSON(id, fallback) {
    const el = document.getElementById(id);
    if (!el) return fallback;
    try { return JSON.parse(el.textContent || "{}"); }
    catch (e) { console.warn(id + " JSON parse failed:", e); return fallback; }
  }

  const handlers = readJSON("sse-handlers", {});
  const tabData = readJSON("tab-data", {});

  // ---------------- drawer ----------------

  const drawer = document.getElementById("row-drawer");
  const drawerBody = document.getElementById("row-drawer-body");
  const drawerTitle = document.getElementById("row-drawer-title");
  const drawerBackdrop = document.getElementById("row-drawer-backdrop");

  function openDrawer(title, bodyHtml) {
    if (!drawer || !drawerBody) return;
    drawerTitle.textContent = title || "Detail";
    drawerBody.innerHTML = bodyHtml;
    drawer.classList.add("open");
    drawerBackdrop.classList.add("open");
  }

  function closeDrawer() {
    if (!drawer) return;
    drawer.classList.remove("open");
    drawerBackdrop.classList.remove("open");
  }

  function drawerLoading() {
    openDrawer("Loading…", '<div class="text-slate-500">Loading detail…</div>');
  }

  async function loadDetail(oppId) {
    drawerLoading();
    // Pick the per-tab detail endpoint. Trades and Opportunities both
    // map opp_id -> detail; the server returns the tab-specific shape.
    let endpoint;
    if (PATH.startsWith("/trades")) {
      endpoint = `/trades/${oppId}/detail`;
    } else {
      endpoint = `/opportunities/${oppId}/detail`;
    }
    try {
      const resp = await fetch(endpoint, { credentials: "same-origin" });
      if (!resp.ok) {
        openDrawer(
          "Error",
          `<div class="text-red-300">Detail fetch failed: ${resp.status}</div>`
        );
        return;
      }
      const body = await resp.json();
      if (PATH.startsWith("/trades")) {
        openDrawer(`Trade — ${body.ticker}`, renderTradeDetail(body));
      } else {
        openDrawer(`Opportunity — ${body.ticker}`, renderOpportunityDetail(body));
      }
    } catch (err) {
      openDrawer(
        "Error",
        `<div class="text-red-300">Network error: ${err && err.message ? err.message : err}</div>`
      );
    }
  }

  function renderOpportunityDetail(d) {
    const book = d.book || {};
    const fees = d.fees || {};
    const sizer = d.sizer || {};
    const orders = d.orders || [];
    const pnl = d.pnl_realized;

    const ordersHtml = orders.length
      ? orders.map(o => `
          <tr class="border-t border-slate-800">
            <td class="px-2 py-1 font-mono text-xs">${escapeHtml(o.client_order_id)}</td>
            <td class="px-2 py-1 text-slate-300">${o.side}</td>
            <td class="px-2 py-1 text-right font-mono">${o.limit_price}</td>
            <td class="px-2 py-1 text-right font-mono">${o.count}</td>
            <td class="px-2 py-1 text-xs font-mono text-slate-500">${o.placed_ts_iso}</td>
            <td class="px-2 py-1">${o.placed_ok
              ? '<span class="text-emerald-300">ok</span>'
              : '<span class="text-red-300">fail</span>'}</td>
            <td class="px-2 py-1 text-xs text-slate-500">${escapeHtml(o.error || '')}</td>
          </tr>
          ${(o.fills || []).map(f => `
            <tr class="bg-slate-900/50">
              <td class="px-2 py-1 text-xs text-slate-500" colspan="2">fill — ${f.filled_ts_iso}</td>
              <td class="px-2 py-1 text-right font-mono">${f.filled_price}</td>
              <td class="px-2 py-1 text-right font-mono">${f.filled_count}</td>
              <td class="px-2 py-1 text-xs text-slate-500" colspan="3">fees ${f.fees_cents}¢</td>
            </tr>
          `).join("")}
        `).join("")
      : '<tr><td class="px-2 py-2 text-slate-500" colspan="7">No orders placed.</td></tr>';

    return `
      <div class="space-y-4">
        <div>
          <div class="text-xs text-slate-500 uppercase">When</div>
          <div class="font-mono text-slate-200">${d.ts_iso}</div>
        </div>
        <div>
          <div class="text-xs text-slate-500 uppercase">Book snapshot</div>
          <div class="mt-1 grid grid-cols-2 gap-2 text-sm">
            <div>YES: ${book.yes_ask_cents}¢ × ${book.yes_ask_qty}</div>
            <div>NO:  ${book.no_ask_cents}¢ × ${book.no_ask_qty}</div>
            <div class="col-span-2 text-slate-400">Sum: ${book.sum_cents}¢</div>
          </div>
        </div>
        <div>
          <div class="text-xs text-slate-500 uppercase">Fee math</div>
          <div class="mt-1 text-sm">
            est fees ${fees.est_fees_cents}¢ — slippage buffer ${fees.slippage_buffer}¢
            — <span class="text-emerald-300">net edge ${Number(fees.net_edge_cents).toFixed(2)}¢</span>
          </div>
        </div>
        <div>
          <div class="text-xs text-slate-500 uppercase">Sizer decomposition</div>
          <div class="mt-1 text-sm">
            liquidity ${sizer.max_size_liquidity} — kelly ${sizer.kelly_size}
            — hard cap ${sizer.hard_cap_size} — <span class="text-cyan-300">final ${sizer.final_size}</span>
          </div>
        </div>
        <div>
          <div class="text-xs text-slate-500 uppercase">Decision</div>
          <div class="mt-1 text-sm">
            <span class="font-mono">${d.decision}</span>
            ${d.rejection_reason ? `<span class="ml-2 text-slate-400">(${escapeHtml(d.rejection_reason)})</span>` : ''}
          </div>
        </div>
        <div>
          <div class="text-xs text-slate-500 uppercase">Orders &amp; fills</div>
          <table class="mt-1 w-full text-sm">
            <thead class="text-xs text-slate-500">
              <tr>
                <th class="px-2 py-1 text-left">COID</th>
                <th class="px-2 py-1 text-left">side</th>
                <th class="px-2 py-1 text-right">price</th>
                <th class="px-2 py-1 text-right">count</th>
                <th class="px-2 py-1 text-left">ts</th>
                <th class="px-2 py-1 text-left">status</th>
                <th class="px-2 py-1 text-left">error</th>
              </tr>
            </thead>
            <tbody>${ordersHtml}</tbody>
          </table>
        </div>
        ${pnl ? `
          <div>
            <div class="text-xs text-slate-500 uppercase">Realized P&amp;L</div>
            <div class="mt-1 text-sm">
              yes ${pnl.yes_pnl_cents}¢ + no ${pnl.no_pnl_cents}¢ − fees ${pnl.fees_cents}¢
              = <span class="font-mono ${pnl.net_cents >= 0 ? 'text-emerald-300' : 'text-red-300'}">${pnl.net_fmt}</span>
            </div>
            <div class="text-xs text-slate-500">${escapeHtml(pnl.note || '')}</div>
          </div>
        ` : '<div class="text-sm text-slate-500">No realized P&amp;L yet (position open or scanner-only).</div>'}
      </div>
    `;
  }

  function renderTradeDetail(d) {
    const legs = d.legs || { yes: {}, no: {} };
    function legHtml(side, leg) {
      const fills = (leg.fills || []).map(f => `
        <li class="text-xs text-slate-400">
          ${f.filled_ts_iso} — ${f.filled_price}¢ × ${f.filled_count}
        </li>`).join("");
      return `
        <div class="rounded border border-slate-800 bg-slate-900 p-3">
          <div class="text-xs text-slate-500 uppercase">${side} leg</div>
          <div class="mt-1 text-sm">
            count ${leg.total_count || 0} — avg fill
            ${leg.avg_fill_price !== null && leg.avg_fill_price !== undefined
              ? Number(leg.avg_fill_price).toFixed(2) + '¢'
              : '—'}
            — fees ${leg.total_fees_cents || 0}¢
          </div>
          ${fills ? '<ul class="mt-2 space-y-1">' + fills + '</ul>' : ''}
        </div>`;
    }
    const opp = renderOpportunityDetail(d);
    return `
      <div class="space-y-4">
        <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
          ${legHtml('YES', legs.yes)}
          ${legHtml('NO', legs.no)}
        </div>
        <div class="rounded border ${d.unwind && d.unwind.needed ? 'border-red-700 bg-red-900/20' : 'border-slate-800 bg-slate-900'} p-3">
          <div class="text-xs text-slate-500 uppercase">Unwind</div>
          <div class="mt-1 text-sm">
            ${d.unwind && d.unwind.needed
              ? `<span class="text-red-300">UNWIND REQUIRED</span> — yes filled ${d.unwind.yes_filled}, no filled ${d.unwind.no_filled}`
              : '<span class="text-emerald-300">Not needed</span> — both legs symmetric'}
          </div>
        </div>
        <hr class="border-slate-800"/>
        ${opp}
      </div>
    `;
  }

  function escapeHtml(s) {
    if (s === null || s === undefined) return "";
    return String(s).replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;",
      '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  // Drawer event wiring (delegated click + ESC + backdrop).
  document.addEventListener("click", (e) => {
    const row = e.target.closest("[data-opp-id]");
    if (row && !e.target.closest("a,button,input,select")) {
      const id = row.getAttribute("data-opp-id");
      if (id) loadDetail(id);
    }
    if (e.target.closest("[data-drawer-close], [data-drawer-backdrop]")) {
      closeDrawer();
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeDrawer();
  });

  // ---------------- filter form wiring ----------------

  function serializeForm(form) {
    const fd = new FormData(form);
    const parts = [];
    for (const [k, v] of fd.entries()) {
      if (v !== "" && v !== null && v !== undefined) {
        parts.push(encodeURIComponent(k) + "=" + encodeURIComponent(v));
      }
    }
    return parts.join("&");
  }

  function hydrateFormFromQuery(form) {
    const params = new URLSearchParams(window.location.search);
    for (const el of form.elements) {
      if (!el.name) continue;
      const v = params.get(el.name);
      if (v !== null) el.value = v;
    }
  }

  function wireFilterForm(id, pathOnSubmit, csvLinkId) {
    const form = document.getElementById(id);
    if (!form) return;
    hydrateFormFromQuery(form);
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const qs = serializeForm(form);
      window.location.href = pathOnSubmit + (qs ? "?" + qs : "");
    });
    const reset = document.getElementById(id + "-reset") || document.getElementById(id.replace("-form", "-reset"));
    if (reset) {
      reset.addEventListener("click", () => {
        window.location.href = pathOnSubmit;
      });
    }
    const csv = document.getElementById(csvLinkId);
    if (csv) {
      const qs = serializeForm(form);
      const base = csv.getAttribute("href").split("?")[0];
      csv.setAttribute("href", base + (qs ? "?" + qs : ""));
      form.addEventListener("change", () => {
        const qs2 = serializeForm(form);
        csv.setAttribute("href", base + (qs2 ? "?" + qs2 : ""));
      });
    }
  }

  wireFilterForm("opps-filter-form", "/opportunities", "opps-csv-link");
  wireFilterForm("trades-filter-form", "/trades", "trades-csv-link");
  const pnlForm = document.getElementById("pnl-time-form");
  if (pnlForm) {
    hydrateFormFromQuery(pnlForm);
    pnlForm.addEventListener("change", () => {
      const qs = serializeForm(pnlForm);
      window.location.href = "/pnl" + (qs ? "?" + qs : "");
    });
  }

  // ---------------- charts ----------------

  function renderOverviewChart() {
    const canvas = document.getElementById("chart-decisions-per-minute");
    if (!canvas || typeof Chart === "undefined") return;
    const points = (tabData.decisions_per_minute || []);
    new Chart(canvas, {
      type: "line",
      data: {
        labels: points.map(p => p.iso),
        datasets: [{
          label: "decisions/min",
          data: points.map(p => p.count),
          borderColor: "#22d3ee",
          backgroundColor: "rgba(34,211,238,0.15)",
          tension: 0.2,
          fill: true,
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: "#64748b", maxRotation: 0, autoSkip: true } },
          y: { ticks: { color: "#64748b" }, beginAtZero: true },
        },
      },
    });
  }

  function renderPnlCharts() {
    const eqCanvas = document.getElementById("chart-equity-curve");
    if (eqCanvas && typeof Chart !== "undefined") {
      const eq = tabData.equity_curve || [];
      const est = tabData.estimated_curve || [];
      new Chart(eqCanvas, {
        type: "line",
        data: {
          datasets: [
            {
              label: "realized",
              data: eq.map(p => ({ x: p.iso, y: p.cum_cents / 100.0 })),
              borderColor: "#34d399",
              backgroundColor: "rgba(52,211,153,0.15)",
              tension: 0.15, fill: false,
            },
            {
              label: "estimated",
              data: est.map(p => ({ x: p.iso, y: p.cum_cents / 100.0 })),
              borderColor: "#a78bfa",
              borderDash: [6, 4],
              tension: 0.15, fill: false,
            },
          ],
        },
        options: {
          parsing: false,
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { labels: { color: "#cbd5e1" } } },
          scales: {
            x: { type: "category", ticks: { color: "#64748b", maxRotation: 0, autoSkip: true } },
            y: { ticks: { color: "#64748b" } },
          },
        },
      });
    }
    const barCanvas = document.getElementById("chart-daily-bars");
    if (barCanvas && typeof Chart !== "undefined") {
      const bars = tabData.daily_bars || [];
      new Chart(barCanvas, {
        type: "bar",
        data: {
          labels: bars.map(b => b.day_iso ? b.day_iso.slice(0, 10) : ""),
          datasets: [{
            label: "$/day",
            data: bars.map(b => b.net_cents / 100.0),
            backgroundColor: bars.map(b => b.net_cents >= 0 ? "#34d39988" : "#ef444488"),
            borderColor: bars.map(b => b.net_cents >= 0 ? "#34d399" : "#ef4444"),
            borderWidth: 1,
          }],
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            x: { ticks: { color: "#64748b" } },
            y: { ticks: { color: "#64748b" } },
          },
        },
      });
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    try { renderOverviewChart(); } catch (e) { console.warn("overview chart fail:", e); }
    try { renderPnlCharts(); } catch (e) { console.warn("pnl chart fail:", e); }
    updateBotStatusBadge();
    startReplicaLagPoll();
  });

  // ---------------- bot status badge in header ----------------

  function updateBotStatusBadge() {
    const badge = document.getElementById("bot-status-badge");
    if (!badge) return;
    // Only the overview tab has bot_status in tabData.
    if (tabData.bot_status) {
      badge.textContent = "bot: " + tabData.bot_status;
      badge.className =
        "rounded px-2 py-0.5 border " +
        (tabData.bot_status === "LIVE" ? "text-emerald-300 border-emerald-800 bg-emerald-900/30"
         : tabData.bot_status === "KILL-SWITCH" ? "text-red-300 border-red-800 bg-red-900/40"
         : tabData.bot_status === "DEGRADED" ? "text-amber-300 border-amber-800 bg-amber-900/40"
         : "text-slate-300 border-slate-800 bg-slate-900");
      return;
    }
    // For other tabs, fetch /overview/data once to set the badge.
    fetch("/overview/data", { credentials: "same-origin" })
      .then(r => r.ok ? r.json() : null)
      .then(j => {
        if (!j) return;
        badge.textContent = "bot: " + j.bot_status;
      })
      .catch(() => {});
  }

  // ---------------- replica-lag indicator (system-health) ----------------

  function startReplicaLagPoll() {
    if (PATH !== "/system-health") return;
    async function tick() {
      try {
        const r = await fetch("/healthz", { credentials: "same-origin" });
        const j = await r.json();
        const lag = j.replica_lag_ms;
        const banner = document.getElementById("replica-warning-banner");
        const el = document.getElementById("hc-replica-lag");
        if (el) el.textContent = (lag === null || lag === undefined) ? "—" : `${lag} ms`;
        if (banner) {
          if (lag !== null && lag !== undefined && lag > 5000) banner.classList.remove("hidden");
          else banner.classList.add("hidden");
        }
      } catch (e) { /* ignore */ }
    }
    tick();
    setInterval(tick, 3000);
  }

  // ---------------- SSE stream + fallback polling ----------------

  let lastSeenId = 0;
  let consecutiveErrors = 0;
  let fallbackTimer = null;
  let eventSource = null;

  // Debounce: many SSE events arriving together should only trigger
  // one refetch per endpoint.
  const pendingRefetches = new Map(); // url -> timeout handle

  function scheduleRefetch(url) {
    if (pendingRefetches.has(url)) return;
    const h = setTimeout(() => {
      pendingRefetches.delete(url);
      performRefetch(url);
    }, 200);
    pendingRefetches.set(url, h);
  }

  async function performRefetch(url) {
    // Two modes:
    //   * url == '__current__' or url == PATH -> reload the page to
    //     re-run SSR (simplest, always consistent with filters).
    //   * anything else -> fetch JSON and patch specific DOM ids.
    if (url === "__current__") {
      // Reload in place, preserving the current querystring.
      window.location.reload();
      return;
    }
    try {
      const r = await fetch(url, { credentials: "same-origin" });
      if (!r.ok) return;
      const body = await r.json();
      patchDomFromData(url, body);
    } catch (e) { /* ignore transient */ }
  }

  function patchDomFromData(url, data) {
    // For now, keep this conservative. The Overview tab is the only
    // one that pulls /overview/data without a full reload; we patch
    // its tiles and the two tickers in-place.
    if (url === "/overview/data") {
      const tiles = data.tiles || {};
      setText("ov-realized", tiles.realized_fmt);
      setText("ov-estimated", tiles.estimated_fmt);
      setText("ov-opps-today", String(tiles.opportunities_today));
      const bot = document.getElementById("overview-bot-status");
      if (bot && data.bot_status) bot.textContent = data.bot_status;
      const badge = document.getElementById("bot-status-badge");
      if (badge && data.bot_status) badge.textContent = "bot: " + data.bot_status;
      // Recent opps / executions
      patchList("ov-recent-opportunities", (data.recent_opportunities || []).map(o => `
        <li class="py-2 flex justify-between items-center" data-opp-id="${o.id}">
          <div>
            <div class="font-mono text-cyan-300">${escapeHtml(o.ticker)}</div>
            <div class="text-xs text-slate-500">${o.ts_iso} — ${escapeHtml(o.decision)}</div>
          </div>
          <div class="font-mono ${o.decision === 'emit' ? 'text-emerald-300' : 'text-slate-500'}">
            edge ${Number(o.net_edge_cents).toFixed(2)}¢ × ${o.final_size}
          </div>
        </li>`).join(""));
      patchList("ov-recent-executions", (data.recent_executions || []).map(e => `
        <li class="py-2 flex justify-between items-center" data-opp-id="${e.opportunity_id}">
          <div>
            <div class="font-mono text-cyan-300">${escapeHtml(e.ticker)}</div>
            <div class="text-xs text-slate-500">${e.ts_iso}</div>
          </div>
          <div class="font-mono text-xs ${e.ok_legs === e.legs ? 'text-emerald-300' : 'text-amber-300'}">
            ${e.ok_legs}/${e.legs} legs ok
          </div>
        </li>`).join(""));
    } else if (url === "/system-health/data") {
      // System-health dynamic parts: probes, ws pool, degraded, ks history.
      window.location.reload();
    } else if (url === "/pnl/data") {
      window.location.reload();
    }
  }

  function setText(id, text) {
    const el = document.getElementById(id);
    if (el && text !== undefined && text !== null) el.textContent = text;
  }

  function patchList(id, html) {
    const el = document.getElementById(id);
    if (el) el.innerHTML = html;
  }

  function onChange(change) {
    if (change.id > lastSeenId) lastSeenId = change.id;
    const h = handlers[change.entity_type];
    if (!h) return;
    if (h.refresh) scheduleRefetch(h.refresh);
  }

  function connectSSE() {
    eventSource = new EventSource("/events/stream");
    eventSource.onopen = () => {
      consecutiveErrors = 0;
      setStatus("SSE: live", "text-emerald-400");
      if (fallbackTimer) {
        clearInterval(fallbackTimer);
        fallbackTimer = null;
      }
    };
    const entityTypes = Object.keys(handlers);
    entityTypes.forEach((t) => {
      eventSource.addEventListener(t, (e) => {
        try { onChange(JSON.parse(e.data)); }
        catch (err) { console.warn("bad SSE payload:", err); }
      });
    });
    eventSource.onerror = () => {
      consecutiveErrors++;
      setStatus("SSE: reconnecting…", "text-amber-400");
      if (consecutiveErrors >= MAX_CONSECUTIVE_ERRORS_BEFORE_FALLBACK) {
        eventSource.close();
        eventSource = null;
        startFallbackPolling();
      }
    };
  }

  async function pollOnce() {
    try {
      const resp = await fetch(
        `/events/poll?since_id=${encodeURIComponent(lastSeenId)}&limit=200`,
        { credentials: "same-origin" }
      );
      if (!resp.ok) throw new Error(`poll ${resp.status}`);
      const body = await resp.json();
      (body.changes || []).forEach((c) => onChange({
        id: c.id, entity_type: c.entity_type, entity_id: c.entity_id,
        ts_ms: c.ts_ms, payload: c.payload,
      }));
      setStatus("SSE: fallback polling (5s)", "text-amber-300");
    } catch (err) {
      setStatus("SSE: offline", "text-red-400");
    }
  }

  function startFallbackPolling() {
    if (fallbackTimer) return;
    setStatus("SSE: fallback polling (5s)", "text-amber-300");
    pollOnce();
    fallbackTimer = setInterval(pollOnce, FALLBACK_POLL_MS);
    setTimeout(() => { if (!eventSource) connectSSE(); }, 60_000);
  }

  if (typeof EventSource === "undefined") {
    startFallbackPolling();
  } else {
    connectSSE();
  }
})();
