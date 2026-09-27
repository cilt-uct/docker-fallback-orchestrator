"use strict";

/*
 * pyca-orchestrator dashboard
 *
 * Sections: config -> utilities -> API client -> state -> rendering
 * (keyed diff against `rowElements`, so a 5s poll patches existing rows
 * instead of rebuilding the table and losing open dropdowns/focus/scroll)
 * -> event wiring (delegated) -> boot.
 *
 * New instance field: add a data-field element to #row-template in
 * index.html, populate it in populateRow(). New row action: add a
 * data-action button to the dropdown menu plus a case in handleRowAction().
 */

// ---------------------------------------------------------------------
// 1. Config
// ---------------------------------------------------------------------

const STATUS_BADGE = {
  running: "text-bg-success",
  starting: "text-bg-warning",
  stopping: "text-bg-warning",
  stopped: "text-bg-secondary",
  error: "text-bg-danger",
};

const STATUS_ICON = {
  running: "bi-check-circle-fill",
  starting: "bi-hourglass-split",
  stopping: "bi-hourglass-split",
  stopped: "bi-slash-circle",
  error: "bi-exclamation-triangle-fill",
};

// Docker status/health strings aren't an enum we control; best-effort groupings, not exhaustive.
const CONTAINER_STATUS_BADGE = {
  running: "text-bg-success",
  restarting: "text-bg-warning",
  paused: "text-bg-warning",
  exited: "text-bg-secondary",
  dead: "text-bg-danger",
  created: "text-bg-secondary",
};

const CONTAINER_HEALTH_BADGE = {
  healthy: "text-bg-success",
  unhealthy: "text-bg-danger",
  starting: "text-bg-warning",
};

const TOAST_ICON = {
  success: "bi-check-circle-fill",
  danger: "bi-exclamation-octagon-fill",
};

const REFRESH_MS = 5000;
const MAX_CONSECUTIVE_FAILURES_BEFORE_BANNER = 1;

// ---------------------------------------------------------------------
// 2. Utilities
// ---------------------------------------------------------------------

function formatBytes(bytes) {
  if (bytes === null || bytes === undefined) return "–";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i++;
  }
  return `${value.toFixed(1)} ${units[i]}`;
}

function formatRelativeTime(isoString) {
  if (!isoString) return "–";
  const date = new Date(isoString.endsWith("Z") ? isoString : `${isoString}Z`);
  if (Number.isNaN(date.getTime())) return "–";
  const diffMs = Date.now() - date.getTime();
  const diffSec = Math.round(diffMs / 1000);
  const divisions = [
    [60, "second"],
    [60, "minute"],
    [24, "hour"],
    [30, "day"],
    [12, "month"],
    [Number.POSITIVE_INFINITY, "year"],
  ];
  let value = diffSec;
  let unit = "second";
  for (const [amount, nextUnit] of divisions) {
    if (Math.abs(value) < amount) break;
    value = Math.round(value / amount);
    unit = nextUnit;
  }
  const rtf = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
  return rtf.format(-value, unit);
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

function showToast(message, variant = "success") {
  const container = document.getElementById("toast-container");
  const toastEl = document.createElement("div");
  toastEl.className = `toast fade align-items-center text-bg-${variant} border-0`;
  toastEl.setAttribute("role", "status");
  toastEl.setAttribute("aria-live", "polite");
  const iconClass = TOAST_ICON[variant] || "bi-info-circle-fill";
  toastEl.innerHTML = `
    <div class="d-flex">
      <div class="toast-body"><i class="bi ${iconClass} me-2" aria-hidden="true"></i>${escapeHtml(message)}</div>
      <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast" aria-label="Close"></button>
    </div>`;
  container.appendChild(toastEl);
  const toast = new bootstrap.Toast(toastEl, { delay: 4500 });
  toast.show();
  toastEl.addEventListener("hidden.bs.toast", () => toastEl.remove());
}

/**
 * Generic accessible replacement for window.confirm(), backed by the
 * #confirm-modal in index.html. Returns a Promise<boolean>.
 */
function confirmAction({ title = "Confirm", body, confirmLabel = "Confirm", danger = true } = {}) {
  return new Promise((resolve) => {
    const modalEl = document.getElementById("confirm-modal");
    const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
    modalEl.querySelector("#confirm-modal-title-text").textContent = title;
    modalEl.querySelector("#confirm-modal-icon").className = `bi ${danger ? "bi-exclamation-triangle text-danger" : "bi-question-circle text-primary"} me-2`;
    modalEl.querySelector("#confirm-modal-body").innerHTML = body;
    const acceptBtn = modalEl.querySelector("#confirm-modal-accept");
    acceptBtn.innerHTML = `<i class="bi ${danger ? "bi-trash3" : "bi-check-lg"} me-1" aria-hidden="true"></i>${escapeHtml(confirmLabel)}`;
    acceptBtn.className = `btn ${danger ? "btn-danger" : "btn-primary"}`;

    let settled = false;
    const cleanup = () => {
      acceptBtn.removeEventListener("click", onAccept);
      modalEl.removeEventListener("hidden.bs.modal", onHidden);
    };
    const onAccept = () => {
      settled = true;
      cleanup();
      modal.hide();
      resolve(true);
    };
    const onHidden = () => {
      cleanup();
      if (!settled) resolve(false);
    };
    acceptBtn.addEventListener("click", onAccept);
    modalEl.addEventListener("hidden.bs.modal", onHidden);
    modal.show();
  });
}

// ---------------------------------------------------------------------
// 3. API client
// ---------------------------------------------------------------------

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch {
      // ignore non-JSON error bodies
    }
    throw new Error(detail);
  }
  if (res.status === 204) return null;
  return res.json();
}

// ---------------------------------------------------------------------
// 4. State
// ---------------------------------------------------------------------

const state = {
  instances: [],
  recordingsByVenue: {},
  filter: { query: "", status: "all" },
  sort: { key: "venue", dir: "asc" },
  consecutiveFailures: 0,
  hasLoadedOnce: false,
};

/** id -> <tr> element, so refreshes patch existing rows instead of rebuilding them. */
const rowElements = new Map();

// ---------------------------------------------------------------------
// 5. Rendering
// ---------------------------------------------------------------------

function renderSummary(summary) {
  document.getElementById("stat-total").textContent = summary.instance_count;
  document.getElementById("stat-running").textContent = summary.by_status.running;

  const errorCount = summary.by_status.error;
  document.getElementById("stat-error").textContent = errorCount;
  document.getElementById("stat-error-card").classList.toggle("attention", errorCount > 0);
  // CSS (.attention, toggled above) handles the chip's color; the glyph itself has to change here.
  document.getElementById("stat-error-icon").className = errorCount > 0 ? "bi bi-exclamation-triangle-fill" : "bi bi-shield-check";

  const disk = summary.disk_usage;
  const diskStat = document.getElementById("stat-disk");
  const diskBar = document.getElementById("disk-progress");
  if (disk) {
    diskStat.textContent = `${formatBytes(disk.used)} / ${formatBytes(disk.total)}`;
    const pct = disk.total ? Math.min(100, (disk.used / disk.total) * 100) : 0;
    diskBar.style.width = `${pct}%`;
    diskBar.className = `progress-bar ${pct > 90 ? "bg-danger" : pct > 75 ? "bg-warning" : "bg-success"}`;
    diskBar.parentElement.setAttribute("aria-valuenow", pct.toFixed(0));
    diskBar.parentElement.setAttribute("aria-valuemin", "0");
    diskBar.parentElement.setAttribute("aria-valuemax", "100");
  } else {
    diskStat.textContent = "–";
    diskBar.style.width = "0%";
  }
}

function getFilteredSortedInstances() {
  const { query, status } = state.filter;
  const q = query.trim().toLowerCase();
  let list = state.instances.filter((inst) => {
    if (status !== "all" && inst.status !== status) return false;
    if (!q) return true;
    return inst.venue.toLowerCase().includes(q) || inst.agent_id.toLowerCase().includes(q);
  });

  const { key, dir } = state.sort;
  const mult = dir === "asc" ? 1 : -1;
  list = list.slice().sort((a, b) => {
    let av;
    let bv;
    if (key === "recordings") {
      av = state.recordingsByVenue[a.venue] ?? -1;
      bv = state.recordingsByVenue[b.venue] ?? -1;
    } else {
      av = a[key];
      bv = b[key];
    }
    if (av < bv) return -1 * mult;
    if (av > bv) return 1 * mult;
    return 0;
  });
  return list;
}

function updateSortIndicators() {
  document.querySelectorAll("#instances-table th[data-sort-key]").forEach((th) => {
    const key = th.dataset.sortKey;
    if (key === state.sort.key) {
      th.setAttribute("aria-sort", state.sort.dir === "asc" ? "ascending" : "descending");
    } else {
      th.setAttribute("aria-sort", "none");
    }
  });
}

function statusLabel(status) {
  return status.charAt(0).toUpperCase() + status.slice(1);
}

function populateRow(tr, inst, bytes) {
  tr.dataset.id = inst.id;
  tr.dataset.status = inst.status;

  tr.querySelector('[data-field="venue"]').textContent = inst.venue;
  tr.querySelector('[data-field="agent_id"]').textContent = inst.agent_id;

  const badge = tr.querySelector('[data-field="status-badge"]');
  const badgeClass = STATUS_BADGE[inst.status] || "text-bg-secondary";
  const badgeIcon = STATUS_ICON[inst.status] || "bi-question-circle";
  badge.className = `badge status-badge ${badgeClass}`;
  badge.innerHTML = `<i class="bi ${badgeIcon}" aria-hidden="true"></i> ${escapeHtml(statusLabel(inst.status))}`;
  if (inst.status === "error") {
    badge.setAttribute("title", inst.last_error || "Unknown error");
  } else {
    badge.removeAttribute("title");
  }

  const errorEl = tr.querySelector('[data-field="last_error"]');
  errorEl.textContent = inst.status === "error" && inst.last_error ? inst.last_error : "";

  const portCell = tr.querySelector('[data-field="ui_port"]');
  portCell.innerHTML = "";
  if (inst.status === "running") {
    const link = document.createElement("a");
    link.href = `http://localhost:${inst.ui_port}`;
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = inst.ui_port;
    portCell.appendChild(link);
  } else {
    portCell.textContent = inst.ui_port;
  }

  const rtspCell = tr.querySelector('[data-field="rtsp_source"]');
  rtspCell.textContent = inst.rtsp_source || "";
  if (!inst.rtsp_source) {
    rtspCell.innerHTML = '<span class="text-secondary">none</span>';
  }

  tr.querySelector('[data-field="recordings"]').textContent = formatBytes(bytes);

  const updatedCell = tr.querySelector('[data-field="updated_at"]');
  updatedCell.textContent = formatRelativeTime(inst.updated_at);
  updatedCell.setAttribute("title", inst.updated_at || "");

  const canStart = inst.status === "stopped" || inst.status === "error";
  const canStop = inst.status === "running";
  const canRestart = inst.status === "running";
  const canDelete = inst.status === "stopped" || inst.status === "error";

  const menu = tr.querySelector('[data-field="actions-menu"]');
  menu.querySelector('[data-action="start"]').disabled = !canStart;
  menu.querySelector('[data-action="stop"]').disabled = !canStop;
  menu.querySelector('[data-action="restart"]').disabled = !canRestart;
  const deleteBtn = menu.querySelector('[data-action="delete"]');
  deleteBtn.disabled = !canDelete;
  deleteBtn.title = canDelete ? "" : "Stop the instance before deleting it";
}

// Total count already lives in the Instances stat card; this is specifically the filtered/matched count.
function updateInstanceCountBadge(shown, total) {
  const badge = document.getElementById("instance-count-badge");
  if (shown === total) {
    badge.textContent = `${total} instance${total === 1 ? "" : "s"}`;
  } else {
    badge.textContent = `Showing ${shown} of ${total}`;
  }
}

function hasActiveFilter() {
  return state.filter.query.trim() !== "" || state.filter.status !== "all";
}

function clearFilters() {
  state.filter.query = "";
  state.filter.status = "all";
  document.getElementById("search-input").value = "";
  document.getElementById("status-filter").value = "all";
  renderTable();
}

function updateClearFiltersButton() {
  document.getElementById("clear-filters-btn").classList.toggle("d-none", !hasActiveFilter());
}

function renderTable() {
  const list = getFilteredSortedInstances();
  const tbody = document.getElementById("instances-body");
  const emptyState = document.getElementById("empty-state");
  const template = document.getElementById("row-template");

  // First render: drop the skeleton placeholder rows.
  tbody.querySelectorAll("tr.skeleton-row").forEach((row) => row.remove());

  updateInstanceCountBadge(list.length, state.instances.length);
  updateClearFiltersButton();

  if (state.instances.length === 0) {
    tbody.innerHTML = "";
    rowElements.clear();
    emptyState.classList.remove("d-none");
    return;
  }
  emptyState.classList.add("d-none");

  if (list.length === 0) {
    tbody.innerHTML = `<tr><td colspan="7" class="text-center text-secondary py-5">
      <i class="bi bi-funnel display-6 d-block mb-2 text-secondary" aria-hidden="true"></i>
      <div class="mb-2">No instances match the current search/filter.</div>
      <button type="button" class="btn btn-sm btn-outline-secondary" id="no-results-clear-btn">
        <i class="bi bi-x-circle" aria-hidden="true"></i> Clear filters
      </button>
    </td></tr>`;
    tbody.querySelector("#no-results-clear-btn").addEventListener("click", clearFilters);
    rowElements.clear();
    return;
  }

  // Remove any stray "no match" row left over from an empty filter result.
  const strayRow = tbody.querySelector("tr:not([data-id])");
  if (strayRow) strayRow.remove();

  const seenIds = new Set();
  let previousEl = null;
  list.forEach((inst) => {
    seenIds.add(String(inst.id));
    let tr = rowElements.get(inst.id);
    let isNew = false;
    if (!tr) {
      tr = template.content.firstElementChild.cloneNode(true);
      rowElements.set(inst.id, tr);
      isNew = true;
    }
    populateRow(tr, inst, state.recordingsByVenue[inst.venue]);

    // Reorder in place rather than rebuild, so open dropdowns/focus on unmoved rows survive.
    const expectedNext = previousEl ? previousEl.nextElementSibling : tbody.firstElementChild;
    if (expectedNext !== tr) {
      if (previousEl) previousEl.after(tr);
      else tbody.prepend(tr);
    }
    if (isNew) tr.classList.add("row-new");
    previousEl = tr;
  });

  // Drop rows for instances that no longer exist / no longer match filters.
  for (const [id, tr] of rowElements) {
    if (!seenIds.has(String(id))) {
      tr.remove();
      rowElements.delete(id);
    }
  }

  updateSortIndicators();
}

// Only for the *initial* load failing (skeleton rows would otherwise be stuck forever). Later poll
// failures keep the last-known-good table and just show the connection banner instead.
function renderLoadError(message) {
  const tbody = document.getElementById("instances-body");
  tbody.innerHTML = "";
  rowElements.clear();
  document.getElementById("instance-count-badge").textContent = "–";
  document.getElementById("empty-state").classList.add("d-none");
  document.getElementById("load-error-message").textContent =
    `The initial request to the backend failed${message ? `: ${message}` : "."}`;
  document.getElementById("load-error-state").classList.remove("d-none");
}

function clearLoadError() {
  document.getElementById("load-error-state").classList.add("d-none");
}

function setLastUpdated(date) {
  // Inner text span, not #last-updated itself, so the leading icon isn't clobbered.
  const el = document.getElementById("last-updated-text");
  el.textContent = `Updated ${date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;
}

function setConnectionBanner(message) {
  const banner = document.getElementById("connection-banner");
  if (message) {
    banner.innerHTML = `<i class="bi bi-wifi-off me-2" aria-hidden="true"></i>${escapeHtml(message)}`;
    banner.classList.remove("d-none");
  } else {
    banner.classList.add("d-none");
  }
}

// ---------------------------------------------------------------------
// Refresh loop
// ---------------------------------------------------------------------

let refreshTimer = null;
let refreshInFlight = false;

async function refresh({ silent = false } = {}) {
  if (refreshInFlight) return;
  refreshInFlight = true;
  const refreshBtn = document.getElementById("refresh-btn");
  refreshBtn.classList.add("is-refreshing");
  try {
    const [instances, summary] = await Promise.all([
      api("/instances"),
      api("/monitoring/summary"),
    ]);
    state.instances = instances;
    state.recordingsByVenue = summary.recordings_bytes_by_instance || {};
    renderSummary(summary);
    clearLoadError();
    renderTable();
    setLastUpdated(new Date());

    if (state.consecutiveFailures > 0 && !silent) {
      showToast("Connection restored", "success");
    }
    state.consecutiveFailures = 0;
    state.hasLoadedOnce = true;
    setConnectionBanner(null);
  } catch (err) {
    state.consecutiveFailures += 1;
    if (state.consecutiveFailures === 1) {
      showToast(`Failed to refresh: ${err.message}`, "danger");
    }
    if (state.consecutiveFailures >= MAX_CONSECUTIVE_FAILURES_BEFORE_BANNER) {
      setConnectionBanner(
        `Live data may be stale – last refresh failed (${err.message}). Retrying every ${REFRESH_MS / 1000}s…`
      );
    }
    if (!state.hasLoadedOnce) {
      renderLoadError(err.message);
    }
  } finally {
    refreshInFlight = false;
    refreshBtn.classList.remove("is-refreshing");
  }
}

function scheduleRefresh() {
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = setInterval(() => refresh({ silent: true }), REFRESH_MS);
}

// ---------------------------------------------------------------------
// 6. Actions
// ---------------------------------------------------------------------

async function doAction(id, action) {
  try {
    await api(`/instances/${id}/${action}`, { method: "POST" });
    showToast(`${statusLabel(action)} succeeded`);
  } catch (err) {
    showToast(`${statusLabel(action)} failed: ${err.message}`, "danger");
  }
  refresh({ silent: true });
}

async function deleteInstance(id, venue) {
  const confirmed = await confirmAction({
    title: "Delete instance",
    body: `Delete the backup instance for <strong>${escapeHtml(venue)}</strong>? This cannot be undone.`,
    confirmLabel: "Delete",
    danger: true,
  });
  if (!confirmed) return;
  try {
    await api(`/instances/${id}`, { method: "DELETE" });
    showToast("Instance deleted");
  } catch (err) {
    showToast(`Delete failed: ${err.message}`, "danger");
  }
  refresh({ silent: true });
}

async function showDetails(id) {
  const modalEl = document.getElementById("details-modal");
  const body = document.getElementById("details-body");
  body.innerHTML = `<div class="text-center py-4"><div class="spinner-border" role="status"><span class="visually-hidden">Loading details…</span></div></div>`;
  bootstrap.Modal.getOrCreateInstance(modalEl).show();

  try {
    const data = await api(`/instances/${id}`);
    const containerRows = data.containers
      .map((c) => {
        const statusClass = CONTAINER_STATUS_BADGE[c.status] || "text-bg-secondary";
        const healthClass = c.health ? CONTAINER_HEALTH_BADGE[c.health] || "text-bg-secondary" : null;
        const healthCell = healthClass
          ? `<span class="badge ${healthClass}">${escapeHtml(c.health)}</span>`
          : '<span class="text-secondary">&ndash;</span>';
        return `<tr>
          <td><code>${escapeHtml(c.name)}</code></td>
          <td><span class="badge ${statusClass}">${escapeHtml(c.status)}</span></td>
          <td>${healthCell}</td>
        </tr>`;
      })
      .join("");
    body.innerHTML = `
      <dl class="row mb-3">
        <dt class="col-sm-4">Compose project</dt><dd class="col-sm-8"><code>${escapeHtml(data.instance.compose_project)}</code></dd>
        <dt class="col-sm-4">Mediapackage</dt><dd class="col-sm-8"><code>${escapeHtml(data.instance.mediapackage_id || "–")}</code></dd>
        <dt class="col-sm-4">Recordings size</dt><dd class="col-sm-8">${formatBytes(data.recordings_bytes)}</dd>
      </dl>
      <table class="table table-sm align-middle">
        <caption class="visually-hidden">Container statuses</caption>
        <thead><tr><th scope="col">Container</th><th scope="col">Status</th><th scope="col">Health</th></tr></thead>
        <tbody>${containerRows || '<tr><td colspan="3" class="text-secondary"><i class="bi bi-inbox me-1" aria-hidden="true"></i>No containers</td></tr>'}</tbody>
      </table>`;
  } catch (err) {
    body.innerHTML = `<div class="alert alert-danger" role="alert"><i class="bi bi-exclamation-octagon-fill me-2" aria-hidden="true"></i>${escapeHtml(err.message)}</div>`;
  }
}

function handleRowAction(action, tr) {
  const id = Number(tr.dataset.id);
  const venue = tr.querySelector('[data-field="venue"]').textContent;
  switch (action) {
    case "start":
    case "stop":
    case "restart":
      doAction(id, action);
      break;
    case "details":
      showDetails(id);
      break;
    case "delete":
      deleteInstance(id, venue);
      break;
    default:
      // Unknown action: no-op, but don't silently swallow during development.
      console.warn(`Unhandled row action: ${action}`);
  }
}

async function createInstance(event) {
  event.preventDefault();
  const form = event.target;

  if (!form.checkValidity()) {
    form.classList.add("was-validated");
    return;
  }

  const payload = {
    venue: form.venue.value.trim(),
    agent_id: form.agent_id.value.trim(),
    rtsp_source: form.rtsp_source.value.trim() || null,
  };

  const submitBtn = form.querySelector('button[type="submit"]');
  const spinner = form.querySelector('[data-role="submit-spinner"]');
  submitBtn.disabled = true;
  spinner.classList.remove("d-none");

  try {
    await api("/instances", { method: "POST", body: JSON.stringify(payload) });
    showToast("Instance created");
    form.reset();
    form.classList.remove("was-validated");
    bootstrap.Modal.getInstance(document.getElementById("create-modal")).hide();
    refresh({ silent: true });
  } catch (err) {
    showToast(`Create failed: ${err.message}`, "danger");
  } finally {
    submitBtn.disabled = false;
    spinner.classList.add("d-none");
  }
}

// ---------------------------------------------------------------------
// 7. Boot / event wiring
// ---------------------------------------------------------------------

function initEvents() {
  document.getElementById("create-form").addEventListener("submit", createInstance);
  document.getElementById("create-modal").addEventListener("hidden.bs.modal", (e) => {
    e.target.querySelector("form").classList.remove("was-validated");
  });

  document.getElementById("refresh-btn").addEventListener("click", () => refresh());
  document.getElementById("load-error-retry").addEventListener("click", () => refresh());

  document.getElementById("search-input").addEventListener("input", (e) => {
    state.filter.query = e.target.value;
    renderTable();
  });

  document.getElementById("status-filter").addEventListener("change", (e) => {
    state.filter.status = e.target.value;
    renderTable();
  });

  document.getElementById("clear-filters-btn").addEventListener("click", clearFilters);

  document.querySelectorAll("#instances-table th[data-sort-key]").forEach((th) => {
    th.querySelector(".th-sort-btn").addEventListener("click", () => {
      const key = th.dataset.sortKey;
      if (state.sort.key === key) {
        state.sort.dir = state.sort.dir === "asc" ? "desc" : "asc";
      } else {
        state.sort.key = key;
        state.sort.dir = "asc";
      }
      renderTable();
    });
  });

  // Delegated: one listener covers every row action, present and future.
  document.getElementById("instances-body").addEventListener("click", (e) => {
    const actionBtn = e.target.closest("[data-action]");
    if (!actionBtn) return;
    const tr = actionBtn.closest("tr[data-id]");
    if (!tr) return;
    handleRowAction(actionBtn.dataset.action, tr);
  });

  // Lifts the table's overflow clip (see style.css) only while a dropdown is open. show.bs.dropdown
  // fires before Popper positions the menu, so Popper sees the un-clipped state already.
  document.addEventListener("show.bs.dropdown", (e) => {
    const wrapper = e.target.closest(".table-responsive");
    if (wrapper) wrapper.classList.add("dropdown-open-in-table");
  });
  document.addEventListener("hide.bs.dropdown", (e) => {
    const wrapper = e.target.closest(".table-responsive");
    if (wrapper) wrapper.classList.remove("dropdown-open-in-table");
  });

  // Pause polling while the tab is hidden; refresh immediately when it becomes visible again.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      if (refreshTimer) clearInterval(refreshTimer);
    } else {
      refresh({ silent: true });
      scheduleRefresh();
    }
  });
}

initEvents();
refresh();
scheduleRefresh();
