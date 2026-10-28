"""Background loop: decides when a venue needs a backup PyCA instance.

A backup is required when, for an upcoming scheduled event:
  - its capture agent hasn't reported in within AGENT_OFFLINE_SECONDS, or
  - the event starts within RECORDING_LEAD_MINUTES and the agent isn't
    in a healthy state (idle/capturing/ingesting).

Mirrors the thresholds used by the existing Node.js fallback-capture-agent
(isAgentHealthy / isUpdateLate in server.js) so the two agents agree on
what "unhealthy" means.
"""

import asyncio
import enum
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from sqlalchemy.orm import Session

from app import audit, compose_manager
from app.config import settings
from app.database import SessionLocal
from app.instance_lifecycle import InstanceNotStartableError, build_instance, provision_and_start
from app.models import AuditOutcome, Instance, InstanceStatus
from app.opencast_client import AgentState, OpencastClient, OpencastError, ScheduledEvent

logger = logging.getLogger(__name__)


class BackupReason(str, enum.Enum):
    """Why the engine started, stopped or kept an instance. Stored in audit event details;
    static/audit.js has a human-readable label for each value."""

    agent_offline = "agent_offline"
    unhealthy_near_start = "unhealthy_near_start"
    no_longer_needed = "no_longer_needed"
    pending_ingest = "pending_ingest"


# Instances whose teardown was deferred on the previous poll, so a recording
# left awaiting ingest is audited once rather than on every poll.
_teardown_deferred_ids: set[int] = set()


def _backup_reason(event: ScheduledEvent, agent_healthy: bool, agent_offline: bool) -> BackupReason | None:
    """Why this event needs a backup instance, or None if it doesn't."""
    if agent_offline:
        return BackupReason.agent_offline
    minutes_to_start = (event.start - datetime.now(UTC)).total_seconds() / 60
    if minutes_to_start <= settings.recording_lead_minutes and not agent_healthy:
        return BackupReason.unhealthy_near_start
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


async def has_pending_ingest(instance: Instance) -> bool:
    """True if the instance's PyCA UI has a recording paused after recording."""
    if not instance.ui_username or not instance.ui_password:
        return False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"http://localhost:{instance.ui_port}/api/events",
                auth=(instance.ui_username, instance.ui_password),
            )
            resp.raise_for_status()
            events = resp.json().get("data", [])
            return any(e.get("attributes", {}).get("status") == "paused after recording" for e in events)
    except (httpx.HTTPError, ValueError, AttributeError, TypeError) as exc:
        logger.warning(
            "Could not check pending ingest for %s, leaving it running to be safe: %s",
            instance.venue,
            exc,
        )
        return True


async def _ensure_backup(
    db: Session, oc: OpencastClient, event: ScheduledEvent, agent_cache: dict[str, AgentState | None]
) -> None:
    """Start a backup instance for one scheduled event if its agent needs one."""
    if event.agent_id not in agent_cache:
        agent_cache[event.agent_id] = await oc.get_agent_state(event.agent_id)
    agent_state = agent_cache[event.agent_id]

    if agent_state is None:
        logger.warning("No agent state for %s, skipping", event.agent_id)
        return

    reason = _backup_reason(event, agent_state.is_healthy, agent_state.is_offline)
    if reason is None:
        return

    instance = _get_or_create_instance(db, event.agent_id, event.agent_id, agent_state.rtsp_source)

    if instance.status == InstanceStatus.running:
        return

    logger.info("Starting backup instance for %s (event %s)", event.agent_id, event.mediapackage_id)
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


async def _tear_down_if_idle(db: Session, instance: Instance, deferred_ids: set[int]) -> None:
    """Stop one instance that no scheduled event needs any more, unless it still holds a recording."""
    if await has_pending_ingest(instance):
        logger.warning(
            "Not stopping backup instance for %s - it has a recording awaiting manual ingest",
            instance.agent_id,
        )
        deferred_ids.add(instance.id)
        if instance.id not in _teardown_deferred_ids:
            audit.record(
                db,
                actor=audit.SYSTEM,
                action=audit.INSTANCE_TEARDOWN_DEFERRED,
                instance=instance,
                detail={
                    "reason": BackupReason.pending_ingest.value,
                    "mediapackage_ids": audit.mediapackages_since_last_stop(db, instance.id),
                },
            )
            db.commit()
        return

    logger.info("Stopping backup instance for %s (no longer needed)", instance.agent_id)
    detail = {
        "reason": BackupReason.no_longer_needed.value,
        "mediapackage_ids": audit.mediapackages_since_last_stop(db, instance.id),
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


async def run_once(oc: OpencastClient) -> None:
    """One poll. Failing to fetch the schedule fails the pass (and is raised);
    a failure on one event or instance is logged and the rest still run."""
    db = SessionLocal()
    try:
        events = await oc.get_scheduled_events()
        agent_cache: dict[str, AgentState | None] = {}

        for event in events:
            try:
                await _ensure_backup(db, oc, event, agent_cache)
            except OpencastError as exc:
                db.rollback()
                logger.warning(
                    "Skipping event %s for %s this poll: %s", event.mediapackage_id, event.agent_id, exc
                )
            except Exception:  # noqa: BLE001 - one venue must not block the others
                db.rollback()
                logger.exception("Failed to handle event %s for %s", event.mediapackage_id, event.agent_id)

        # Tear down instances whose agent has recovered and has no near-term event.
        # "starting"/"stopping" here are leftovers from a pass that died mid-transition
        # (crash, lost DB connection); `compose down` is idempotent, so finish the job.
        active_agent_ids = {e.agent_id for e in events}
        candidate_ids = [
            instance.id
            for instance in db.query(Instance).filter(
                Instance.status.in_(
                    [InstanceStatus.running, InstanceStatus.starting, InstanceStatus.stopping]
                )
            )
            if instance.agent_id not in active_agent_ids
        ]
        deferred_ids: set[int] = set()
        for instance_id in candidate_ids:
            try:
                instance = db.get(Instance, instance_id)
                if instance is not None:
                    await _tear_down_if_idle(db, instance, deferred_ids)
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
