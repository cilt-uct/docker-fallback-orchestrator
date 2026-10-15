"use strict";

/* Helpers shared by the dashboard (app.js) and the audit log page (audit.js). */

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
