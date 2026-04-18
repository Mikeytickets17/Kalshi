// Dashboard client.
//
// Connects to /events/stream via EventSource. If the connection drops
// repeatedly, falls back to polling /events/poll every 5 s. The status
// indicator in the footer reflects live / reconnecting / fallback.
//
// Each tab's template declares a <template id="sse-handlers"> block
// containing event-type -> element-id mappings; we register
// addEventListener() for each. Missing entries are fine -- tabs that
// don't care about an event type just ignore it.
//
// Wire format (from kalshi_arb.dashboard.sse.Change.as_sse_event):
//   event: opportunity
//   id: 12345
//   data: {"id":12345,"entity_type":"opportunity","entity_id":67,
//          "ts_ms":1700000000000,"payload":null}

(function () {
  "use strict";

  const STATUS_EL_ID = "sse-status";
  const FALLBACK_POLL_MS = 5000;
  const MAX_CONSECUTIVE_ERRORS_BEFORE_FALLBACK = 3;

  const statusEl = document.getElementById(STATUS_EL_ID);
  const setStatus = (text, cls) => {
    if (!statusEl) return;
    statusEl.textContent = text;
    statusEl.className = cls || "";
  };

  // Track the last seen change id so we can resume after reconnect or
  // switch to fallback polling without missing rows.
  let lastSeenId = 0;
  let consecutiveErrors = 0;
  let fallbackTimer = null;
  let eventSource = null;

  // Read the per-tab event-type -> DOM element mapping from a JSON
  // <script type="application/json" id="sse-handlers"> block.
  const handlers = (() => {
    const blob = document.getElementById("sse-handlers");
    if (!blob) return {};
    try {
      return JSON.parse(blob.textContent || "{}");
    } catch (e) {
      console.warn("sse-handlers JSON parse failed:", e);
      return {};
    }
  })();

  function onChange(change) {
    if (change.id > lastSeenId) lastSeenId = change.id;
    const handler = handlers[change.entity_type];
    if (!handler) return; // this tab doesn't care about this event type
    // Default behavior: update a counter element with the new id, so
    // operator sees the stream is alive. Step 5 replaces per-tab
    // handlers with real row rendering.
    const target = document.getElementById(handler.target);
    if (target) {
      target.setAttribute("data-latest-id", change.id);
      target.textContent = handler.label
        ? `${handler.label}: last id ${change.id}`
        : `last id ${change.id}`;
    }
  }

  function connectSSE() {
    // EventSource auto-retries on network drop; it also sends
    // Last-Event-ID on reconnect so the server replays missed rows.
    eventSource = new EventSource("/events/stream");
    eventSource.onopen = () => {
      consecutiveErrors = 0;
      setStatus("SSE: live", "text-emerald-400");
      if (fallbackTimer) {
        clearInterval(fallbackTimer);
        fallbackTimer = null;
      }
    };
    // Register a per-type listener for each key of 'handlers'. Plus a
    // catch-all 'message' listener for legacy events without 'event:'.
    const entityTypes = Object.keys(handlers);
    entityTypes.forEach((t) => {
      eventSource.addEventListener(t, (e) => {
        try {
          onChange(JSON.parse(e.data));
        } catch (err) {
          console.warn("bad SSE payload:", err);
        }
      });
    });
    eventSource.onerror = () => {
      consecutiveErrors++;
      setStatus("SSE: reconnecting...", "text-amber-400");
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
      (body.changes || []).forEach(onChange);
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
    // Try to reconnect SSE every minute while in fallback mode.
    setTimeout(() => {
      if (!eventSource) connectSSE();
    }, 60_000);
  }

  // Entry
  if (typeof EventSource === "undefined") {
    // Very old browser -- polling only.
    startFallbackPolling();
  } else {
    connectSSE();
  }
})();
