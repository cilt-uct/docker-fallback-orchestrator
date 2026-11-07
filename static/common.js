"use strict";

/* Helpers shared by the dashboard (app.js), audit log (audit.js) and recordings (recordings.js) pages,
   including the pagination controls every table uses.
   showToast and confirmAction need the Bootstrap JS bundle on the page; confirmAction also needs
   the #confirm-modal markup. */

/** The API returns naive UTC timestamps (no offset), so say so before parsing. */
function parseUtc(isoString) {
  if (!isoString) return null;
  const date = new Date(isoString.endsWith("Z") ? isoString : `${isoString}Z`);
  return Number.isNaN(date.getTime()) ? null : date;
}

function formatRelativeTime(isoString) {
  const date = parseUtc(isoString);
  if (!date) return "–";
  const diffMs = Date.now() - date.getTime();
  const diffSec = Math.round(diffMs / 1000);
  // [how many of the current unit make one of the next, the next unit's name]
  const divisions = [
    [60, "minute"],
    [60, "hour"],
    [24, "day"],
    [30, "month"],
    [12, "year"],
  ];
  let value = diffSec;
  let unit = "second";
  for (const [amount, nextUnit] of divisions) {
    if (Math.abs(value) < amount) break;
    // Truncate, not round: 90 minutes is "1 hour ago", not "2 hours ago".
    value = Math.trunc(value / amount);
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

async function api(path, options = {}) {
  const res = await apiResponse(path, options);
  if (res.status === 204) return null;
  return res.json();
}

/** GET a page from an endpoint that reports its total in X-Total-Count. Returns {items, total}. */
async function apiPage(path) {
  const res = await apiResponse(path);
  const items = await res.json();
  const total = Number(res.headers.get("X-Total-Count"));
  return { items, total: Number.isFinite(total) ? total : items.length };
}

/** fetch() that throws an Error with the server's message (and error reference) on failure. */
async function apiResponse(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (Array.isArray(body.detail)) {
        // FastAPI validation errors: a list of {loc, msg}, not a string.
        detail = body.detail
          .map((item) => {
            const field = Array.isArray(item.loc) ? item.loc[item.loc.length - 1] : null;
            return field && field !== "body" ? `${field}: ${item.msg}` : item.msg;
          })
          .join("; ");
      } else {
        detail = body.detail || detail;
      }
      // Reference for an unexpected server error, to match against the server log.
      if (body.error_id) detail += ` (ref ${body.error_id})`;
    } catch {
      // ignore non-JSON error bodies
    }
    throw new Error(detail);
  }
  return res;
}

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

const TOAST_ICON = {
  success: "bi-check-circle-fill",
  danger: "bi-exclamation-octagon-fill",
};

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
// Pagination controls, shared by every table
// ---------------------------------------------------------------------

const PAGE_SIZES = [10, 25, 50, 100];
const DEFAULT_PAGE_SIZE = 25;

/** The viewer's chosen page size for a table, remembered in this browser only. */
function loadPageSize(table) {
  try {
    const size = Number(localStorage.getItem(`pageSize:${table}`));
    return PAGE_SIZES.includes(size) ? size : DEFAULT_PAGE_SIZE;
  } catch {
    return DEFAULT_PAGE_SIZE;
  }
}

function savePageSize(table, size) {
  try {
    localStorage.setItem(`pageSize:${table}`, String(size));
  } catch {
    // storage unavailable (private window, blocked) - the choice just isn't remembered
  }
}

function pageCount(total, pageSize) {
  return Math.max(1, Math.ceil(total / pageSize));
}

/** Page numbers to show: first, last, and the current page's neighbours, with gaps as null. */
function pageNumbers(page, pages) {
  const wanted = new Set([1, pages, page - 1, page, page + 1].filter((n) => n >= 1 && n <= pages));
  const sorted = [...wanted].sort((a, b) => a - b);
  const out = [];
  sorted.forEach((n, i) => {
    if (i > 0 && n - sorted[i - 1] > 1) out.push(null);
    out.push(n);
  });
  return out;
}

/**
 * Render "Showing a-b of N", a page-size choice and page links into `container`.
 * onChange({ page, pageSize }) is called when the viewer picks a page or a size.
 */
function renderPagination(container, { page, pageSize, total, noun = "items", onChange }) {
  if (total === 0) {
    container.innerHTML = "";
    container.classList.add("d-none");
    return;
  }
  container.classList.remove("d-none");
  const pages = pageCount(total, pageSize);
  const first = (page - 1) * pageSize + 1;
  const last = Math.min(total, page * pageSize);
  const sizeId = `${container.id}-size`;

  const item = (label, target, { disabled = false, active = false, aria = "" } = {}) => `
    <li class="page-item${disabled ? " disabled" : ""}${active ? " active" : ""}">
      <button type="button" class="page-link" data-page="${target}" ${disabled ? "disabled" : ""}
        ${active ? 'aria-current="page"' : ""} ${aria ? `aria-label="${aria}"` : ""}>${label}</button>
    </li>`;
  const links = pageNumbers(page, pages)
    .map((n) =>
      n === null
        ? '<li class="page-item disabled"><span class="page-link">&hellip;</span></li>'
        : item(n, n, { active: n === page, aria: `Page ${n}` })
    )
    .join("");

  container.innerHTML = `
    <div class="d-flex flex-wrap align-items-center justify-content-between gap-2">
      <span class="text-secondary small">Showing ${first}&ndash;${last} of ${total} ${escapeHtml(noun)}</span>
      <div class="d-flex flex-wrap align-items-center gap-2">
        <label for="${sizeId}" class="text-secondary small">Per page</label>
        <select id="${sizeId}" class="form-select form-select-sm page-size-select">
          ${PAGE_SIZES.map((n) => `<option value="${n}"${n === pageSize ? " selected" : ""}>${n}</option>`).join("")}
        </select>
        <nav aria-label="Pages">
          <ul class="pagination pagination-sm mb-0">
            ${item('<i class="bi bi-chevron-left" aria-hidden="true"></i>', page - 1, { disabled: page <= 1, aria: "Previous page" })}
            ${links}
            ${item('<i class="bi bi-chevron-right" aria-hidden="true"></i>', page + 1, { disabled: page >= pages, aria: "Next page" })}
          </ul>
        </nav>
      </div>
    </div>`;

  container.querySelectorAll("[data-page]").forEach((btn) =>
    btn.addEventListener("click", () => onChange({ page: Number(btn.dataset.page), pageSize }))
  );
  container
    .querySelector(".page-size-select")
    .addEventListener("change", (e) => onChange({ page: 1, pageSize: Number(e.target.value) }));
}
