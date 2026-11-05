"use strict";

/*
 * pyca-orchestrator audit log page
 *
 * Reads GET /audit, newest first. Filters live in the URL query string, so a
 * filtered view (e.g. the dashboard's per-instance "View history" link) can
 * be linked to and survives a reload. Older events are paged in with
 * before_id; new ones are polled for and prepended, so rows already on
 * screen (and the scroll position) are left alone.
 */

const PAGE_SIZE = 50;
const POLL_MS = 10000;
const FILTER_KEYS = ["venue", "action", "actor_type", "outcome"];

const ACTION_LABEL = {
  "instance.create": "Created",
  "instance.start": "Started",
  "instance.stop": "Stopped",
  "instance.restart": "Restarted",
  "instance.delete": "Deleted",
  "instance.rtsp_source_changed": "RTSP source changed",
  "instance.teardown_deferred": "Teardown deferred",
};

// One entry per BackupReason in app/decision_engine.py, plus retired values that
// older audit events still carry.
const REASON_LABEL = {
  agent_offline: "Agent offline",
  agent_unhealthy: "Agent unhealthy",
  recently_unhealthy: "Agent was down within 24h of the lecture",
  no_longer_needed: "Agent recovered - backup no longer needed",
  recording_in_progress: "Backup is recording",
  pending_ingest: "Recording awaiting ingest or uploading",
  pyca_unreachable: "Backup's PyCA could not be reached",
  unhealthy_near_start: "Agent unhealthy close to recording start", // retired
};

const DETAIL_LABEL = {
  mediapackage_id: "Mediapackage",
  mediapackage_ids: "Mediapackages",
  rtsp_source: "RTSP source",
  ui_port: "UI port",
  agent_healthy_since: "Agent healthy since (UTC)",
  last_needed_lecture_end: "Last lecture that needed it ended (UTC)",
  from: "From",
  to: "To",
};

const form = document.getElementById("audit-filters");
const tbody = document.getElementById("audit-body");
const message = document.getElementById("audit-message");
const loadMoreWrap = document.getElementById("load-more-wrap");

let oldestId = null;
let newestId = null;
let requestSeq = 0;
let pollTimer = null;

// Exact local time is the primary value - "x ago" alone is too coarse for an audit trail.
const dateFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "medium" });
const timeFormat = new Intl.DateTimeFormat(undefined, { timeStyle: "medium" });

// Date and time are separate no-wrap chunks so that on a phone the cell breaks between them, not mid-date.
function formatExactTime(isoString) {
  const date = parseUtc(isoString);
  if (!date) return "–";
  return `<span class="text-nowrap">${escapeHtml(dateFormat.format(date))},</span> <span class="text-nowrap">${escapeHtml(timeFormat.format(date))}</span>`;
}

function updateRelativeTimes() {
  tbody.querySelectorAll("[data-occurred-at]").forEach((el) => {
    el.textContent = formatRelativeTime(el.dataset.occurredAt);
  });
}

function currentFilters() {
  const filters = {};
  FILTER_KEYS.forEach((key) => {
    const value = form.elements[key].value.trim();
    if (value) filters[key] = value;
  });
  return filters;
}

function renderDetail(detail) {
  if (!detail) return "";
  const parts = [];
  if (detail.reason) parts.push(`<div>${escapeHtml(REASON_LABEL[detail.reason] || detail.reason)}</div>`);
  if (detail.error) parts.push(`<div class="text-danger">${escapeHtml(detail.error)}</div>`);
  Object.entries(detail).forEach(([key, value]) => {
    if (key === "reason" || key === "error" || value === null || value === "") return;
    if (Array.isArray(value) && value.length === 0) return;
    const values = Array.isArray(value) ? value : [value];
    parts.push(
      `<div class="text-secondary">${escapeHtml(DETAIL_LABEL[key] || key)}: ${values
        .map((v) => `<code>${escapeHtml(String(v))}</code>`)
        .join(" ")}</div>`
    );
  });
  return parts.join("");
}

function renderRow(event) {
  const failed = event.outcome === "failure";
  const actor =
    event.actor_type === "system"
      ? '<i class="bi bi-cpu me-1" aria-hidden="true"></i>System'
      : `<i class="bi bi-person me-1" aria-hidden="true"></i>${escapeHtml(event.actor || "Operator")}` +
        (event.source_ip ? `<div class="text-secondary small">${escapeHtml(event.source_ip)}</div>` : "");
  // Actor and Details columns are hidden below md; the facts that matter (who, and why it failed) move under the action.
  const who = event.actor_type === "system" ? "System" : event.actor || "Operator";
  const phoneSummary =
    `<div class="d-md-none text-secondary small">${escapeHtml(who)}</div>` +
    (failed && event.detail && event.detail.error
      ? `<div class="d-md-none text-danger small text-break">${escapeHtml(event.detail.error)}</div>`
      : "");
  const tr = document.createElement("tr");
  tr.innerHTML = `
    <td class="text-md-nowrap" title="${escapeHtml(event.occurred_at)} UTC">
      <div>${formatExactTime(event.occurred_at)}</div>
      <div class="text-secondary small text-nowrap" data-occurred-at="${escapeHtml(event.occurred_at)}">${escapeHtml(formatRelativeTime(event.occurred_at))}</div>
    </td>
    <td>
      ${escapeHtml(ACTION_LABEL[event.action] || event.action)}
      ${failed ? '<span class="badge text-bg-danger ms-1"><i class="bi bi-exclamation-triangle-fill" aria-hidden="true"></i> Failed</span>' : ""}
      ${phoneSummary}
    </td>
    <td class="text-md-nowrap">
      <div class="fw-semibold">${escapeHtml(event.venue || "–")}</div>
      <div class="text-secondary small">${escapeHtml(event.agent_id || "")}</div>
    </td>
    <td class="d-none d-md-table-cell text-nowrap">${actor}</td>
    <td class="d-none d-md-table-cell small text-break audit-detail">${renderDetail(event.detail)}</td>`;
  return tr;
}

function showMessage(html) {
  message.innerHTML = html;
  message.classList.toggle("d-none", !html);
}

async function load({ append = false } = {}) {
  const seq = ++requestSeq;
  const filters = currentFilters();
  const hasFilters = Object.keys(filters).length > 0;
  const params = new URLSearchParams({ ...filters, limit: PAGE_SIZE });
  if (append && oldestId !== null) params.set("before_id", oldestId);

  const refreshBtn = document.getElementById("refresh-btn");
  refreshBtn.classList.add("is-refreshing");
  try {
    const events = await api(`/audit?${params}`);
    if (seq !== requestSeq) return; // a newer filter change superseded this request
    if (!append) {
      tbody.innerHTML = "";
      newestId = events.length ? events[0].id : null;
    }
    events.forEach((event) => tbody.appendChild(renderRow(event)));
    if (events.length) oldestId = events[events.length - 1].id;
    loadMoreWrap.classList.toggle("d-none", events.length < PAGE_SIZE);
    showMessage(
      tbody.children.length
        ? ""
        : `<i class="bi bi-inbox display-5 d-block mb-2" aria-hidden="true"></i>
           <p class="mb-0 fs-5">${hasFilters ? "No events match these filters" : "Nothing has been recorded yet"}</p>`
    );
  } catch (err) {
    if (seq !== requestSeq) return;
    if (!append) tbody.innerHTML = "";
    loadMoreWrap.classList.add("d-none");
    showMessage(
      `<i class="bi bi-exclamation-octagon display-5 d-block mb-2 text-danger" aria-hidden="true"></i>
       <p class="mb-0 fs-5 text-danger">Couldn&rsquo;t load the audit log: ${escapeHtml(err.message)}</p>`
    );
  } finally {
    if (seq === requestSeq) refreshBtn.classList.remove("is-refreshing");
  }
}

/** Prepend anything recorded since the newest row on screen, and keep "x ago" current. */
async function poll() {
  updateRelativeTimes();
  const seq = requestSeq;
  try {
    const events = await api(`/audit?${new URLSearchParams({ ...currentFilters(), limit: PAGE_SIZE })}`);
    if (seq !== requestSeq) return; // filters changed or a full load ran meanwhile
    const fresh = events.filter((event) => newestId === null || event.id > newestId);
    if (!fresh.length) return;
    if (fresh.length === events.length && newestId !== null) {
      // A whole page of new events: there may be a gap below it, so reload instead of prepending.
      oldestId = null;
      load();
      return;
    }
    fresh.reverse().forEach((event) => {
      const tr = renderRow(event);
      tr.classList.add("row-new");
      tbody.prepend(tr);
    });
    newestId = events[0].id;
    if (oldestId === null) oldestId = events[events.length - 1].id;
    showMessage("");
  } catch {
    // Background poll: stay quiet, the next tick or a manual Refresh will surface a real outage.
  }
}

function schedulePoll() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(poll, POLL_MS);
}

function applyFilters() {
  const filters = currentFilters();
  const query = new URLSearchParams(filters).toString();
  history.replaceState(null, "", query ? `?${query}` : location.pathname);
  document.getElementById("clear-filters-btn").classList.toggle("d-none", !query);
  oldestId = null;
  load();
}

function init() {
  const params = new URLSearchParams(location.search);
  FILTER_KEYS.forEach((key) => {
    const field = form.elements[key];
    field.value = params.get(key) || "";
    // An unknown value in the URL leaves a <select> blank; fall back to "All".
    if (field.tagName === "SELECT" && field.selectedIndex === -1) field.value = "";
  });

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    applyFilters();
  });
  form.addEventListener("change", applyFilters);
  document.getElementById("filter-venue").addEventListener("search", applyFilters);
  document.getElementById("clear-filters-btn").addEventListener("click", () => {
    form.reset();
    applyFilters();
  });
  document.getElementById("refresh-btn").addEventListener("click", () => {
    oldestId = null;
    load();
  });
  document.getElementById("load-more-btn").addEventListener("click", () => load({ append: true }));

  // Pause polling while the tab is hidden; catch up immediately when it becomes visible again.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      if (pollTimer) clearInterval(pollTimer);
    } else {
      poll();
      schedulePoll();
    }
  });

  applyFilters();
  schedulePoll();
}

init();
