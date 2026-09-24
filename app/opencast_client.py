"""Thin client for the two Opencast APIs the orchestrator needs:
Capture Agent state (capture-admin) and the event scheduler (admin-ng).

Mirrors the request shape used by the existing Node.js fallback-capture-agent
(see cilt-uct/fallback-capture-agent server.js) so behaviour stays consistent
across both agents.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from app.config import settings


def _iso8601(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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

    async def get_scheduled_events(self) -> list[ScheduledEvent]:
        now = datetime.now(timezone.utc)
        # startDate only bounds technical_start; drop finished events below instead.
        earliest = now - timedelta(days=1)
        later = now + timedelta(days=settings.schedule_lookahead_days)
        date_range = f"{_iso8601(earliest)}/{_iso8601(later)}"
        filter_str = f"status:EVENTS.EVENTS.STATUS.SCHEDULED,startDate:{date_range}"

        resp = await self._client.get(
            "/admin-ng/event/events.json",
            params={"filter": filter_str},
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])

        events = []
        for event in results:
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
        return events

    async def get_agent_state(self, agent_id: str) -> AgentState | None:
        resp = await self._client.get(f"/capture-admin/agents/{agent_id}.json")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()

        update = resp.json()["agent-state-update"]
        capabilities = (update.get("capabilities") or {}).get("item") or []
        rtsp_source = next(
            (
                item["value"]
                for item in capabilities
                if item.get("key") == "capture.device.presenter.src"
            ),
            None,
        )

        return AgentState(
            agent_id=agent_id,
            state=update["state"],
            seconds_since_update=float(update["time-since-last-update"]) / 1000,
            rtsp_source=rtsp_source,
        )
