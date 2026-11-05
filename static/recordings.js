"use strict";

/*
 * pyca-orchestrator recordings page
 *
 * Lists every backup recording on disk (GET /recordings) and lets an operator
 * change how long each is kept (PUT /recordings/{venue}/{name}/retention) or
 * delete it now (DELETE /recordings/{venue}/{name}).
 * The server's clean-up task deletes a recording once its retention has passed
 * since it started - ingested or not - so this is where a recording gets saved
 * from deletion. Filters live in the URL, like the audit log page, so an
 * instance's "View recordings" link can open it pre-filtered.
 */

const FILTER_KEYS = ["venue", "status"];

// PyCA statuses (app/recordings.py PYCA_STATUS) grouped for display.
const STATUS_DISPLAY = {
  "finished uploading": { label: "Ingested", badge: "text-bg-success", icon: "bi-check-circle-fill", group: "ingested" },
  recording: { label: "Recording", badge: "text-bg-info", icon: "bi-record-circle", group: "in-progress" },
  uploading: { label: "Uploading", badge: "text-bg-info", icon: "bi-cloud-upload", group: "in-progress" },
  "failed recording": { label: "Failed recording", badge: "text-bg-secondary", icon: "bi-x-circle", group: "not-ingested" },
};
const NOT_INGESTED = { label: "Not ingested", badge: "text-bg-warning", icon: "bi-exclamation-triangle-fill", group: "not-ingested" };

const form = document.getElementById("recording-filters");
const tbody = document.getElementById("recordings-body");
const message = document.getElementById("recordings-message");

const timeFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

let recordings = [];
let defaultRetentionDays = null;

function statusDisplay(pycaStatus) {
  return STATUS_DISPLAY[pycaStatus] || NOT_INGESTED;
}

function formatTime(isoString) {
  const date = parseUtc(isoString);
  return date ? timeFormat.format(date) : "–";
}

function currentFilters() {
  const filters = {};
  FILTER_KEYS.forEach((key) => {
    if (form.elements[key].value) filters[key] = form.elements[key].value;
  });
  return filters;
}

function showMessage(html) {
  message.innerHTML = html;
  message.classList.toggle("d-none", !html);
}

function populateVenueFilter() {
  const select = form.elements.venue;
  const wanted = new URLSearchParams(location.search).get("venue") || select.value;
  const venues = [...new Set(recordings.map((r) => r.venue))].sort();
  if (wanted && !venues.includes(wanted)) venues.unshift(wanted); // keep a linked-to venue selectable
  select.innerHTML =
    '<option value="">All venues</option>' +
    venues.map((v) => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join("");
  select.value = wanted || "";
}

function renderRetentionCell(rec) {
  const hint = `<span class="text-secondary small">${rec.retention_overridden ? "custom" : "default"}</span>`;
  return `
    <form class="d-flex align-items-center gap-1 retention-form" data-action="save">
      <label class="visually-hidden" for="ret-${escapeHtml(rec.name)}">Days to keep ${escapeHtml(rec.name)}</label>
      <input id="ret-${escapeHtml(rec.name)}" type="number" class="form-control form-control-sm retention-input"
             min="1" max="3650" required value="${rec.retention_days}" data-original="${rec.retention_days}">
      <span class="text-secondary small">days</span>
      <button type="submit" class="btn btn-primary btn-sm d-none" data-role="save">Save</button>
    </form>
    <div>${hint}</div>`;
}

function renderDeletedCell(rec) {
  if (rec.in_progress && parseUtc(rec.delete_after) <= new Date()) {
    return '<span class="text-secondary small">Once it finishes</span>';
  }
  return `<div>${escapeHtml(formatTime(rec.delete_after))}</div>
    <div class="text-secondary small">${escapeHtml(formatRelativeTime(rec.delete_after))}</div>`;
}

function renderRow(rec) {
  const status = statusDisplay(rec.pyca_status);
  const tr = document.createElement("tr");
  tr.dataset.venue = rec.venue;
  tr.dataset.name = rec.name;
  tr.innerHTML = `
    <td class="text-md-nowrap">
      <div>${escapeHtml(formatTime(rec.started_at))}</div>
      <div class="text-secondary small"><code>${escapeHtml(rec.mediapackage_id || rec.name)}</code></div>
    </td>
    <td class="fw-semibold">${escapeHtml(rec.venue)}</td>
    <td>
      <span class="badge ${status.badge}" title="PyCA status: ${escapeHtml(rec.pyca_status)}">
        <i class="bi ${status.icon}" aria-hidden="true"></i> ${escapeHtml(status.label)}
      </span>
    </td>
    <td class="d-none d-md-table-cell text-nowrap">${escapeHtml(formatBytes(rec.size_bytes))}</td>
    <td>${renderRetentionCell(rec)}</td>
    <td class="d-none d-sm-table-cell text-nowrap">${renderDeletedCell(rec)}</td>
    <td class="text-end">
      <button type="button" class="btn btn-sm btn-outline-danger" data-action="delete" aria-label="Delete recording"
              ${rec.in_progress ? 'disabled title="Still being recorded or uploaded"' : ""}>
        <i class="bi bi-trash3" aria-hidden="true"></i><span class="d-none d-lg-inline"> Delete</span>
      </button>
    </td>`;
  return tr;
}

function render() {
  const { venue, status } = currentFilters();
  const shown = recordings.filter(
    (r) => (!venue || r.venue === venue) && (!status || statusDisplay(r.pyca_status).group === status)
  );
  tbody.innerHTML = "";
  shown.forEach((rec) => tbody.appendChild(renderRow(rec)));
  document.getElementById("recording-count").textContent =
    shown.length === recordings.length
      ? `${recordings.length} recording${recordings.length === 1 ? "" : "s"}`
      : `Showing ${shown.length} of ${recordings.length}`;
  showMessage(
    shown.length
      ? ""
      : `<i class="bi bi-inbox display-5 d-block mb-2" aria-hidden="true"></i>
         <p class="mb-0 fs-5">${recordings.length ? "No recordings match these filters" : "No backup recordings on disk"}</p>`
  );
}

async function load() {
  const refreshBtn = document.getElementById("refresh-btn");
  refreshBtn.classList.add("is-refreshing");
  try {
    const [settings, list] = await Promise.all([api("/recordings/settings"), api("/recordings")]);
    defaultRetentionDays = settings.default_retention_days;
    document.getElementById("retention-note").textContent =
      `Deleted ${defaultRetentionDays} days after recording unless changed below - ingested or not.`;
    recordings = list;
    populateVenueFilter();
    render();
  } catch (err) {
    tbody.innerHTML = "";
    showMessage(
      `<i class="bi bi-exclamation-octagon display-5 d-block mb-2 text-danger" aria-hidden="true"></i>
       <p class="mb-0 fs-5 text-danger">Couldn&rsquo;t load recordings: ${escapeHtml(err.message)}</p>`
    );
  } finally {
    refreshBtn.classList.remove("is-refreshing");
  }
}

async function saveRetention(tr, retentionDays) {
  const { venue, name } = tr.dataset;
  try {
    const updated = await api(`/recordings/${encodeURIComponent(venue)}/${encodeURIComponent(name)}/retention`, {
      method: "PUT",
      body: JSON.stringify({ retention_days: retentionDays }),
    });
    recordings = recordings.map((r) => (r.venue === venue && r.name === name ? updated : r));
    render();
    showToast(`Will be kept for ${retentionDays} days from recording`);
  } catch (err) {
    showToast(`Couldn't change retention: ${err.message}`, "danger");
  }
}

async function deleteRecording(tr) {
  const rec = recordings.find((r) => r.venue === tr.dataset.venue && r.name === tr.dataset.name);
  if (!rec) return;
  const notIngested = statusDisplay(rec.pyca_status).group !== "ingested";
  const confirmed = await confirmAction({
    title: "Delete recording",
    body: `Delete the <strong>${escapeHtml(rec.venue)}</strong> recording from
      <strong>${escapeHtml(formatTime(rec.started_at))}</strong> (${escapeHtml(formatBytes(rec.size_bytes))})?
      This cannot be undone.
      ${
        notIngested
          ? `<div class="alert alert-warning mt-3 mb-0" role="alert">
               <i class="bi bi-exclamation-triangle-fill me-1" aria-hidden="true"></i>
               This recording was <strong>not ingested</strong> into Opencast - it may be the only copy.
             </div>`
          : ""
      }`,
    confirmLabel: "Delete",
    danger: true,
  });
  if (!confirmed) return;
  try {
    await api(`/recordings/${encodeURIComponent(rec.venue)}/${encodeURIComponent(rec.name)}`, { method: "DELETE" });
    recordings = recordings.filter((r) => r !== rec);
    populateVenueFilter();
    render();
    showToast("Recording deleted");
  } catch (err) {
    showToast(`Couldn't delete: ${err.message}`, "danger");
  }
}

function applyFilters() {
  const query = new URLSearchParams(currentFilters()).toString();
  history.replaceState(null, "", query ? `?${query}` : location.pathname);
  document.getElementById("clear-filters-btn").classList.toggle("d-none", !query);
  render();
}

function init() {
  const params = new URLSearchParams(location.search);
  form.elements.status.value = params.get("status") || "";
  if (form.elements.status.selectedIndex === -1) form.elements.status.value = "";
  document.getElementById("clear-filters-btn").classList.toggle("d-none", !params.toString());

  form.addEventListener("change", applyFilters);
  document.getElementById("clear-filters-btn").addEventListener("click", () => {
    form.reset();
    form.elements.venue.value = "";
    applyFilters();
  });
  document.getElementById("refresh-btn").addEventListener("click", load);

  // Show Save only once the number differs from what's stored.
  tbody.addEventListener("input", (e) => {
    const input = e.target.closest(".retention-input");
    if (!input) return;
    input.closest("form").querySelector('[data-role="save"]').classList.toggle("d-none", input.value === input.dataset.original);
  });
  tbody.addEventListener("click", (e) => {
    if (e.target.closest('[data-action="delete"]')) deleteRecording(e.target.closest("tr"));
  });
  tbody.addEventListener("submit", (e) => {
    e.preventDefault();
    const input = e.target.querySelector(".retention-input");
    if (!input.reportValidity()) return;
    saveRetention(e.target.closest("tr"), Number(input.value));
  });

  load();
}

init();
