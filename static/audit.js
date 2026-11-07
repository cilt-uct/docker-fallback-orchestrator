"use strict";

/*
 * pyca-orchestrator audit log page
 *
 * Reads GET /audit, newest first, a page at a time (limit + offset; the total
 * comes from the X-Total-Count header). Filters and the page live in the URL
 * query string, so a filtered view (e.g. the dashboard's per-instance "View
 * history" link) can be linked to and survives a reload. The first page is
 * refreshed every few seconds, with new events highlighted; other pages stay
 * still so rows don't shift while they're being read - only the total updates.
 */

const POLL_MS = 10000;
const FILTER_KEYS = ["venue", "action", "actor_type", "outcome", "since"];
const SUMMARY_DAYS = 7;

// What each summary tile filters the table to (on top of its time window and the venue filter).
const TILE_FILTERS = {
  failures: { outcome: "failure" },
  backups: { action: "instance.start", actor_type: "system", outcome: "success" },
  manual: { actor_type: "operator" },
  deleted: { action: "recording.deleted" },
};

const ACTION_LABEL = {
  "instance.create": "Created",
  "instance.start": "Started",
  "instance.stop": "Stopped",
  "instance.restart": "Restarted",
  "instance.delete": "Deleted",
  "instance.rtsp_source_changed": "RTSP source changed",
  "instance.teardown_deferred": "Teardown deferred",
  "instance.containers_lost": "Containers found not running",
  "recording.deleted": "Recording deleted",
  "recording.retention_changed": "Recording retention changed",
};

// One entry per BackupReason in app/decision_engine.py, plus retired values that
// older audit events still carry.
const REASON_LABEL = {
  agent_offline: "Agent offline",
  agent_unhealthy: "Agent unhealthy",
  recently_unhealthy: "Agent was down within 24h of the lecture",
  no_longer_needed: "Agent recovered - backup no longer needed",
  recording_in_progress: "Backup is recording",
  ingest_in_progress: "Backup is ingesting a recording",
  started_by_operator: "Started by hand - stays up until stopped",
  pending_ingest: "Recording awaiting ingest or uploading", // retired
  pyca_unreachable: "Backup's PyCA could not be reached",
  unhealthy_near_start: "Agent unhealthy close to recording start", // retired
};

const DETAIL_LABEL = {
  mediapackage_id: "Mediapackage",
  mediapackage_ids: "Mediapackages",
  rtsp_source: "RTSP source",
  ui_port: "UI port",
  agent_healthy_since: "Agent healthy since (UTC)",
  recording: "Recording",
  pyca_status: "PyCA status",
  started_at: "Started (UTC)",
  retention_days: "Retention (days)",
  size_bytes: "Size (bytes)",
  last_needed_lecture_end: "Last lecture that needed it ended (UTC)",
  from: "From",
  to: "To",
};

const form = document.getElementById("audit-filters");
const tbody = document.getElementById("audit-body");
const message = document.getElementById("audit-message");
const paginationEl = document.getElementById("audit-pagination");

let page = 1;
let pageSize = loadPageSize("audit");
let shownIds = new Set(); // ids on screen, to highlight genuinely new events after a refresh
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

/** The summary tiles: counts for the last SUMMARY_DAYS, for the venue filter if one is set. */
async function loadSummary() {
  const venue = form.elements.venue.value.trim();
  const params = new URLSearchParams({ days: SUMMARY_DAYS });
  if (venue) params.set("venue", venue);
  let summary;
  try {
    summary = await api(`/audit/summary?${params}`);
  } catch {
    return; // the table shows any outage; the tiles just keep their last values
  }
  const set = (id, text) => {
    document.getElementById(id).textContent = text;
  };
  const scope = venue ? `${venue}, last ${summary.days} days` : `last ${summary.days} days`;

  set("tile-failures-value", summary.failures);
  set("tile-failures-sub", scope);
  const failing = summary.failures > 0;
  document.getElementById("tile-failures").classList.toggle("attention", failing);
  const chip = document.getElementById("tile-failures-chip").classList;
  chip.toggle("bg-secondary-subtle", !failing);
  chip.toggle("text-secondary-emphasis", !failing);
  chip.toggle("bg-danger-subtle", failing);
  chip.toggle("text-danger-emphasis", failing);
  document.querySelector("#tile-failures-chip i").className =
    `bi ${failing ? "bi-exclamation-triangle-fill" : "bi-shield-check"}`;

  set("tile-backups-value", summary.backups_started);
  set(
    "tile-backups-sub",
    venue ? scope : `across ${summary.backup_venues} venue${summary.backup_venues === 1 ? "" : "s"}`
  );
  set("tile-manual-value", summary.manual_actions);
  set("tile-manual-sub", scope);
  set("tile-deleted-value", summary.recordings_deleted);
  set("tile-deleted-sub", summary.bytes_freed ? `${formatBytes(summary.bytes_freed)} freed` : scope);

  document.querySelectorAll("[data-tile]").forEach((tile) => {
    tile.dataset.since = summary.since;
  });
}

/** Clicking a tile shows exactly what it counted: its filter, its time window, the same venue. */
function applyTile(tile) {
  const venue = form.elements.venue.value;
  form.reset();
  form.elements.venue.value = venue;
  form.elements.since.value = tile.dataset.since || "";
  Object.entries(TILE_FILTERS[tile.dataset.tile]).forEach(([key, value]) => {
    form.elements[key].value = value;
  });
  applyFilters();
}

function renderSinceChip() {
  const since = form.elements.since.value;
  document.getElementById("since-chip").classList.toggle("d-none", !since);
  if (since) document.getElementById("since-chip-text").textContent = `Since ${formatTimePlain(since)}`;
}

function formatTimePlain(isoString) {
  const date = parseUtc(isoString);
  return date ? `${dateFormat.format(date)}, ${timeFormat.format(date)}` : isoString;
}

function renderAuditPagination(total) {
  renderPagination(paginationEl, {
    page,
    pageSize,
    total,
    noun: total === 1 ? "event" : "events",
    onChange: (next) => {
      page = next.page;
      if (next.pageSize !== pageSize) savePageSize("audit", next.pageSize);
      pageSize = next.pageSize;
      updateUrl();
      load();
    },
  });
}

function pageQuery() {
  return new URLSearchParams({ ...currentFilters(), limit: pageSize, offset: (page - 1) * pageSize });
}

/** Load the current page. `quiet` (the background refresh) keeps the table on errors. */
async function load({ quiet = false } = {}) {
  const seq = ++requestSeq;
  const hasFilters = Object.keys(currentFilters()).length > 0;
  const refreshBtn = document.getElementById("refresh-btn");
  if (!quiet) refreshBtn.classList.add("is-refreshing");
  try {
    let { items, total } = await apiPage(`/audit?${pageQuery()}`);
    if (seq !== requestSeq) return; // a newer filter or page change superseded this request
    const pages = pageCount(total, pageSize);
    if (page > pages) {
      // The page went away (events filtered out, or fewer than before) - show the last one instead.
      page = pages;
      updateUrl();
      ({ items, total } = await apiPage(`/audit?${pageQuery()}`));
      if (seq !== requestSeq) return;
    }
    const previouslyShown = shownIds;
    tbody.innerHTML = "";
    items.forEach((event) => {
      const tr = renderRow(event);
      if (quiet && !previouslyShown.has(event.id)) tr.classList.add("row-new");
      tbody.appendChild(tr);
    });
    shownIds = new Set(items.map((event) => event.id));
    renderAuditPagination(total);
    showMessage(
      items.length
        ? ""
        : `<i class="bi bi-inbox display-5 d-block mb-2" aria-hidden="true"></i>
           <p class="mb-0 fs-5">${hasFilters ? "No events match these filters" : "Nothing has been recorded yet"}</p>`
    );
  } catch (err) {
    if (seq !== requestSeq || quiet) return; // background refresh: the next tick or Refresh will retry
    tbody.innerHTML = "";
    shownIds = new Set();
    renderAuditPagination(0);
    showMessage(
      `<i class="bi bi-exclamation-octagon display-5 d-block mb-2 text-danger" aria-hidden="true"></i>
       <p class="mb-0 fs-5 text-danger">Couldn&rsquo;t load the audit log: ${escapeHtml(err.message)}</p>`
    );
  } finally {
    if (seq === requestSeq) refreshBtn.classList.remove("is-refreshing");
  }
}

/** Keep "x ago" current; refresh the first page, where new events appear, and the tiles. */
async function poll() {
  updateRelativeTimes();
  loadSummary();
  if (page === 1) await load({ quiet: true });
}

function schedulePoll() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(poll, POLL_MS);
}

function updateUrl() {
  const params = new URLSearchParams(currentFilters());
  if (page > 1) params.set("page", page);
  const query = params.toString();
  history.replaceState(null, "", query ? `?${query}` : location.pathname);
}

function applyFilters() {
  page = 1; // a different filter starts from its first page
  updateUrl();
  renderSinceChip();
  loadSummary(); // the tiles follow the venue filter
  document.getElementById("clear-filters-btn").classList.toggle("d-none", !Object.keys(currentFilters()).length);
  shownIds = new Set();
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
    loadSummary();
    load();
  });
  document.querySelectorAll("[data-tile]").forEach((tile) =>
    tile.addEventListener("click", () => applyTile(tile))
  );
  document.getElementById("since-chip-clear").addEventListener("click", () => {
    form.elements.since.value = "";
    applyFilters();
  });

  // Pause polling while the tab is hidden; catch up immediately when it becomes visible again.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      if (pollTimer) clearInterval(pollTimer);
    } else {
      poll();
      schedulePoll();
    }
  });

  document.getElementById("clear-filters-btn").classList.toggle("d-none", !Object.keys(currentFilters()).length);
  page = Math.max(1, Number.parseInt(params.get("page"), 10) || 1);
  renderSinceChip();
  loadSummary();
  load();
  schedulePoll();
}

init();
