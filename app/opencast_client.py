"""Thin client for the two Opencast APIs the orchestrator needs:
Capture Agent state (capture-admin) and the event scheduler (admin-ng).

Mirrors the request shape used by the existing Node.js fallback-capture-agent
(see cilt-uct/fallback-capture-agent server.js) so behaviour stays consistent
across both agents.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from app.config import settings

logger = logging.getLogger("opencast_client")


class OpencastError(RuntimeError):
    """Opencast couldn't be reached, refused the request, or sent something unusable."""


def _iso8601(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _extract_rtsp_source(capabilities: dict | None) -> str | None:
    # Opencast serialises a single-item "item" as a bare object instead of a
    # one-element list - only multi-capability agents get a real list.
    items = (capabilities or {}).get("item") or []
    if isinstance(items, dict):
        items = [items]
    return next(
        (item["value"] for item in items if item.get("key") == "capture.device.presenter.src"),
        None,
    )


@dataclass
class ScheduledEvent:
    mediapackage_id: str
    title: str
    agent_id: str
    start: datetime
    end: datetime


@dataclass
class AgentState:
    agent_id: str
    state: str
    seconds_since_update: float
    rtsp_source: str | None

    @property
    def is_healthy(self) -> bool:
        return self.state in ("idle", "capturing", "ingesting")

    @property
    def is_offline(self) -> bool:
        return self.seconds_since_update > settings.agent_offline_seconds


class OpencastClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.opencast_url,
            auth=httpx.DigestAuth(settings.opencast_username, settings.opencast_password),
            headers={"X-Requested-Auth": "Digest", "Accept": "application/json"},
            timeout=30.0,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get_json(self, path: str, *, allow_404: bool = False, **kwargs):
        try:
            resp = await self._client.get(path, **kwargs)
            if allow_404 and resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            raise OpencastError(f"Opencast returned HTTP {exc.response.status_code} for {path}") from exc
        except httpx.HTTPError as exc:
            raise OpencastError(f"Could not reach Opencast for {path}: {exc!r}") from exc
        except ValueError as exc:
            raise OpencastError(f"Opencast sent a non-JSON response for {path}") from exc

    async def get_scheduled_events(self) -> list[ScheduledEvent]:
        now = datetime.now(timezone.utc)
        # startDate only bounds technical_start; drop finished events below instead.
        earliest = now - timedelta(days=1)
        later = now + timedelta(days=settings.schedule_lookahead_days)
        date_range = f"{_iso8601(earliest)}/{_iso8601(later)}"
        filter_str = f"status:EVENTS.EVENTS.STATUS.SCHEDULED,startDate:{date_range}"

        body = await self._get_json("/admin-ng/event/events.json", params={"filter": filter_str})
        if not isinstance(body, dict) or not isinstance(body.get("results", []), list):
            raise OpencastError("Opencast sent an unexpected shape for the scheduled events list")

        events = []
        for event in body.get("results", []):
            # One malformed event must not hide every other venue's schedule.
            try:
                agent_id = event.get("agent_id")
                if not agent_id:
                    continue
                end = datetime.fromisoformat(event["technical_end"])
                if end < now:
                    continue
                events.append(
                    ScheduledEvent(
                        mediapackage_id=event["id"],
                        title=event.get("title", ""),
                        agent_id=agent_id,
                        start=datetime.fromisoformat(event["technical_start"]),
                        end=end,
                    )
                )
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                logger.warning("Skipping malformed scheduled event %r: %r", event, exc)
        return events

    async def get_agent_state(self, agent_id: str) -> AgentState | None:
        body = await self._get_json(f"/capture-admin/agents/{agent_id}.json", allow_404=True)
        if body is None:
            return None

        try:
            update = body["agent-state-update"]
            return AgentState(
                agent_id=agent_id,
                state=update["state"],
                seconds_since_update=float(update["time-since-last-update"]) / 1000,
                rtsp_source=_extract_rtsp_source(update.get("capabilities")),
            )
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise OpencastError(f"Opencast sent an unusable state for agent {agent_id}: {exc!r}") from exc
