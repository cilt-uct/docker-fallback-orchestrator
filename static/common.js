"use strict";

/* Helpers shared by the dashboard (app.js), audit log (audit.js) and recordings (recordings.js) pages.
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
  if (res.status === 204) return null;
  return res.json();
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
