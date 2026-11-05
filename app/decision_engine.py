"""Background loop: decides when a venue needs a backup PyCA instance.

Each pass observes every relevant capture agent and records what it saw in
`agent_health`, because the rules look back in time (AGENT_STABLE_HOURS and
BACKUP_RETENTION_DAYS default to 24h and 4 days):

Start - a scheduled lecture needs a backup if its agent is offline or
unhealthy now, or was at any point in the AGENT_STABLE_HOURS before the
lecture starts - even if it has since recovered. A backup is started as soon
as such a lecture is in the schedule window.

Teardown - a running backup is only stopped once ALL of these hold:
  - its agent has been continuously healthy for AGENT_STABLE_HOURS,
  - BACKUP_RETENTION_DAYS have passed since the end of the last lecture
    that needed it, and
  - its PyCA isn't recording, holding a recording awaiting ingest, or
    uploading one.

"Healthy" uses the same agent states as the Node.js fallback-capture-agent
(isAgentHealthy / isUpdateLate in server.js), plus "uploading".
"""

import asyncio
import enum
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from app import audit, compose_manager
from app.config import settings
from app.database import SessionLocal
from app.instance_lifecycle import InstanceNotStartableError, build_instance, provision_and_start
from app.models import AgentHealth, AuditOutcome, Instance, InstanceStatus, utcnow
from app.opencast_client import AgentState, OpencastClient, OpencastError, ScheduledEvent

logger = logging.getLogger(__name__)


class BackupReason(str, enum.Enum):
    """Why the engine started, stopped or kept an instance. Stored in audit event details;
    static/audit.js has a human-readable label for each value."""

    # Why a lecture needs a backup.
    agent_offline = "agent_offline"
    agent_unhealthy = "agent_unhealthy"
    recently_unhealthy = "recently_unhealthy"
    # Why a backup was stopped, or held back from stopping.
    no_longer_needed = "no_longer_needed"
    recording_in_progress = "recording_in_progress"
    pending_ingest = "pending_ingest"
    pyca_unreachable = "pyca_unreachable"


# PyCA event statuses that mean the backup still holds a recording someone needs.
PYCA_RECORDING_STATUSES = {"recording"}
PYCA_UNFINISHED_STATUSES = {
    "finished recording",
    "partial recording",
    "paused after recording",
    "uploading",
    "failed uploading",
}

# "starting"/"stopping" are included as leftovers from a pass that died mid-transition
# (crash, lost DB connection); `compose down` is idempotent, so they're treated like
# running ones.
ACTIVE_STATUSES = (InstanceStatus.running, InstanceStatus.starting, InstanceStatus.stopping)

# Instances whose teardown was deferred on the previous poll, so a recording
# left awaiting ingest is audited once rather than on every poll.
_teardown_deferred_ids: set[int] = set()


def _stable_window() -> timedelta:
    return timedelta(hours=settings.agent_stable_hours)


def _naive_utc(dt: datetime) -> datetime:
    """Opencast times are timezone-aware; the database stores naive UTC (see models.utcnow)."""
    return dt.astimezone(UTC).replace(tzinfo=None)


def observe_agent(db: Session, state: AgentState, now: datetime) -> AgentHealth:
    """Record one observation of an agent's health and return its updated history."""
    health = db.get(AgentHealth, state.agent_id)
    if health is None:
        health = AgentHealth(agent_id=state.agent_id, last_checked_at=now)
        db.add(health)
        db.flush()  # so a second observation before commit finds this row instead of adding another

    if state.is_healthy and not state.is_offline:
        # A gap in observations (controller down, Opencast unreachable) means we can't
        # vouch for the agent during it, so the healthy run restarts from now.
        gap = now - health.last_checked_at > timedelta(seconds=3 * settings.poll_interval_seconds)
        if health.healthy_since is None or gap:
            health.healthy_since = now
    else:
        health.last_unhealthy_at = now
        health.healthy_since = None
    health.last_checked_at = now
    return health


def _backup_reason(event: ScheduledEvent, state: AgentState, health: AgentHealth) -> BackupReason | None:
    """Why this lecture needs a backup, or None if it doesn't."""
    if state.is_offline:
        return BackupReason.agent_offline
    if not state.is_healthy:
        return BackupReason.agent_unhealthy
    # Recovered now, but down within the stable window before the lecture starts.
    if health.last_unhealthy_at and health.last_unhealthy_at >= _naive_utc(event.start) - _stable_window():
        return BackupReason.recently_unhealthy
    return None


def _teardown_blocker(health: AgentHealth | None, now: datetime) -> str | None:
    """Why a backup must stay up on the agent's account, or None if it may be stopped."""
    if health is None or health.healthy_since is None:
        return "agent not healthy"
    if now - health.healthy_since < _stable_window():
        return f"agent healthy for less than {settings.agent_stable_hours:g}h"
    retention = timedelta(days=settings.backup_retention_days)
    if health.last_needed_event_end_at and now - health.last_needed_event_end_at < retention:
        return f"within {settings.backup_retention_days:g} days of the last lecture that needed it"
    return None


def _get_or_create_instance(db: Session, venue: str, agent_id: str, rtsp_source: str | None) -> Instance:
    instance = db.query(Instance).filter_by(venue=venue).one_or_none()
    if instance:
        if rtsp_source and instance.rtsp_source != rtsp_source:
            audit.record(
                db,
                actor=audit.SYSTEM,
                action=audit.INSTANCE_RTSP_SOURCE_CHANGED,
                instance=instance,
                detail={
                    "from": audit.redact_url(instance.rtsp_source),
                    "to": audit.redact_url(rtsp_source),
                },
            )
            instance.rtsp_source = rtsp_source
        return instance

    instance = build_instance(db, venue, agent_id, rtsp_source)
    db.add(instance)
    db.flush()
    audit.record(
        db,
        actor=audit.SYSTEM,
        action=audit.INSTANCE_CREATE,
        instance=instance,
        detail={"rtsp_source": audit.redact_url(rtsp_source), "ui_port": instance.ui_port},
    )
    return instance


@dataclass
class EngineStatus:
    """Outcome of recent polls, for /monitoring/ready. Only a whole-pass failure counts as an error."""

    last_success_at: datetime | None = None
    last_error_at: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0


status = EngineStatus()


async def pyca_activity(instance: Instance) -> BackupReason | None:
    """What the instance's own PyCA is still doing that a teardown would interrupt, if anything."""
    if not instance.ui_username or not instance.ui_password:
        return None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"http://localhost:{instance.ui_port}/api/events",
                auth=(instance.ui_username, instance.ui_password),
            )
            resp.raise_for_status()
            statuses = {e.get("attributes", {}).get("status") for e in resp.json().get("data", [])}
    except (httpx.HTTPError, ValueError, AttributeError, TypeError) as exc:
        logger.warning("Could not reach PyCA for %s, leaving it running to be safe: %s", instance.venue, exc)
        return BackupReason.pyca_unreachable
    if statuses & PYCA_RECORDING_STATUSES:
        return BackupReason.recording_in_progress
    if statuses & PYCA_UNFINISHED_STATUSES:
        return BackupReason.pending_ingest
    return None


async def _ensure_backup(db: Session, event: ScheduledEvent, state: AgentState, health: AgentHealth) -> None:
    """Start a backup instance for one scheduled lecture if it needs one."""
    reason = _backup_reason(event, state, health)
    if reason is None:
        return

    event_end = _naive_utc(event.end)
    if health.last_needed_event_end_at is None or event_end > health.last_needed_event_end_at:
        health.last_needed_event_end_at = event_end

    instance = _get_or_create_instance(db, event.agent_id, event.agent_id, state.rtsp_source)

    if instance.status == InstanceStatus.running:
        db.commit()
        return

    logger.info(
        "Starting backup instance for %s (event %s, %s)", event.agent_id, event.mediapackage_id, reason
    )
    detail = {"reason": reason.value, "mediapackage_id": event.mediapackage_id}
    # A failed start is retried every poll - only audit it when the error changes.
    previous_error = instance.last_error if instance.status == InstanceStatus.error else None
    instance.status = InstanceStatus.starting
    db.commit()
    try:
        provision_and_start(instance)
    except Exception as exc:  # noqa: BLE001 - surfaced via last_error
        if isinstance(exc, (InstanceNotStartableError, compose_manager.ComposeError)):
            logger.error("Failed to start backup for %s: %s", event.agent_id, exc)
        else:
            logger.exception("Failed to start backup for %s", event.agent_id)
        instance.status = InstanceStatus.error
        instance.last_error = str(exc)
        if str(exc) != previous_error:
            audit.record(
                db,
                actor=audit.SYSTEM,
                action=audit.INSTANCE_START,
                outcome=AuditOutcome.failure,
                instance=instance,
                detail={**detail, "error": str(exc)},
            )
    else:
        instance.status = InstanceStatus.running
        instance.last_error = None
        audit.record(db, actor=audit.SYSTEM, action=audit.INSTANCE_START, instance=instance, detail=detail)
    db.commit()


async def _tear_down_if_done(
    db: Session, instance: Instance, health: AgentHealth | None, now: datetime, deferred_ids: set[int]
) -> None:
    """Stop one backup if its agent has recovered for long enough and nothing it holds would be lost."""
    if blocker := _teardown_blocker(health, now):
        logger.debug("Keeping backup instance for %s: %s", instance.agent_id, blocker)
        return

    if activity := await pyca_activity(instance):
        logger.warning("Not stopping backup instance for %s yet: %s", instance.agent_id, activity.value)
        deferred_ids.add(instance.id)
        if instance.id not in _teardown_deferred_ids:
            audit.record(
                db,
                actor=audit.SYSTEM,
                action=audit.INSTANCE_TEARDOWN_DEFERRED,
                instance=instance,
                detail={
                    "reason": activity.value,
                    "mediapackage_ids": audit.mediapackages_since_last_stop(db, instance.id),
                },
            )
            db.commit()
        return

    logger.info("Stopping backup instance for %s (agent recovered)", instance.agent_id)
    detail = {
        "reason": BackupReason.no_longer_needed.value,
        "mediapackage_ids": audit.mediapackages_since_last_stop(db, instance.id),
        "agent_healthy_since": health.healthy_since.isoformat(),
        "last_needed_lecture_end": (
            health.last_needed_event_end_at.isoformat() if health.last_needed_event_end_at else None
        ),
    }
    instance.status = InstanceStatus.stopping
    db.commit()
    try:
        compose_manager.down(instance)
    except Exception as exc:  # noqa: BLE001 - surfaced via last_error
        if isinstance(exc, compose_manager.ComposeError):
            logger.error("Failed to stop backup for %s: %s", instance.agent_id, exc)
        else:
            logger.exception("Failed to stop backup for %s", instance.agent_id)
        instance.status = InstanceStatus.error
        instance.last_error = str(exc)
        audit.record(
            db,
            actor=audit.SYSTEM,
            action=audit.INSTANCE_STOP,
            outcome=AuditOutcome.failure,
            instance=instance,
            detail={**detail, "error": str(exc)},
        )
    else:
        instance.status = InstanceStatus.stopped
        audit.record(db, actor=audit.SYSTEM, action=audit.INSTANCE_STOP, instance=instance, detail=detail)
    db.commit()


async def _observe_agents(
    db: Session, oc: OpencastClient, agent_ids: set[str], now: datetime
) -> dict[str, tuple[AgentState, AgentHealth]]:
    """Fetch and record the current state of each agent. Agents Opencast doesn't know, or
    whose state can't be fetched this pass, are left out - no decision is made about them."""
    observed = {}
    for agent_id in sorted(agent_ids):
        try:
            state = await oc.get_agent_state(agent_id)
        except OpencastError as exc:
            logger.warning("Skipping agent %s this poll: %s", agent_id, exc)
            continue
        if state is None:
            logger.warning("No agent state for %s, skipping", agent_id)
            continue
        observed[agent_id] = (state, observe_agent(db, state, now))
    db.commit()
    return observed


async def run_once(oc: OpencastClient) -> None:
    """One poll. Failing to fetch the schedule fails the pass (and is raised);
    a failure on one event or instance is logged and the rest still run."""
    db = SessionLocal()
    try:
        events = await oc.get_scheduled_events()
        now = utcnow()

        active = db.query(Instance).filter(Instance.status.in_(ACTIVE_STATUSES)).all()
        active_ids = [instance.id for instance in active]
        # Agents with a backup are observed even with nothing scheduled: their teardown
        # depends on how long they've been healthy.
        agent_ids = {e.agent_id for e in events} | {instance.agent_id for instance in active}
        observed = await _observe_agents(db, oc, agent_ids, now)

        for event in events:
            if event.agent_id not in observed:
                continue
            state, health = observed[event.agent_id]
            try:
                await _ensure_backup(db, event, state, health)
            except Exception:  # noqa: BLE001 - one venue must not block the others
                db.rollback()
                logger.exception("Failed to handle event %s for %s", event.mediapackage_id, event.agent_id)

        deferred_ids: set[int] = set()
        for instance_id in active_ids:
            try:
                instance = db.get(Instance, instance_id)
                # Its status may have changed in the start phase above (e.g. a failed restart).
                if instance is None or instance.status not in ACTIVE_STATUSES:
                    continue
                if instance.agent_id not in observed:
                    continue  # no current state for its agent - make no decision
                _, health = observed[instance.agent_id]
                await _tear_down_if_done(db, instance, health, now, deferred_ids)
            except Exception:  # noqa: BLE001 - one venue must not block the others
                db.rollback()
                logger.exception("Failed to tear down instance %s", instance_id)
        _teardown_deferred_ids.clear()
        _teardown_deferred_ids.update(deferred_ids)
    finally:
        db.close()


async def run_forever() -> None:
    oc = OpencastClient()
    try:
        while True:
            try:
                await run_once(oc)
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                status.consecutive_failures += 1
                status.last_error_at = datetime.now(UTC)
                status.last_error = str(exc) or repr(exc)
                if isinstance(exc, OpencastError):
                    logger.error("Decision engine poll failed: %s", exc)
                else:
                    logger.exception("Decision engine poll failed")
            else:
                status.consecutive_failures = 0
                status.last_success_at = datetime.now(UTC)
            await asyncio.sleep(settings.poll_interval_seconds)
    finally:
        await oc.aclose()
