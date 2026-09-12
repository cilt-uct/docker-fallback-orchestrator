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
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app import compose_manager, pyca_config
from app.config import settings
from app.database import SessionLocal
from app.models import Instance, InstanceStatus
from app.opencast_client import OpencastClient, ScheduledEvent
from app.port_allocator import allocate_port

logger = logging.getLogger("decision_engine")


def _needs_backup(event: ScheduledEvent, agent_healthy: bool, agent_offline: bool) -> bool:
    if agent_offline:
        return True
    minutes_to_start = (event.start - datetime.now(timezone.utc)).total_seconds() / 60
    return minutes_to_start <= settings.recording_lead_minutes and not agent_healthy


def _get_or_create_instance(db: Session, venue: str, agent_id: str, rtsp_source: str | None) -> Instance:
    instance = db.query(Instance).filter_by(venue=venue).one_or_none()
    if instance:
        if rtsp_source and instance.rtsp_source != rtsp_source:
            instance.rtsp_source = rtsp_source
        return instance

    instance = Instance(
        venue=venue,
        agent_id=agent_id,
        ui_port=allocate_port(db),
        compose_project=f"pyca-{venue}",
        config_dir=str(settings.instances_dir / venue),
        rtsp_source=rtsp_source,
        status=InstanceStatus.stopped,
    )
    db.add(instance)
    db.flush()
    return instance


def _provision_and_start(instance: Instance) -> None:
    ui_username, ui_password = pyca_config.generate_ui_credentials()
    pyca_config.write_pyca_conf(instance, ui_username, ui_password)
    compose_manager.render_compose_file(instance)
    compose_manager.up(instance)


async def run_once(oc: OpencastClient) -> None:
    db = SessionLocal()
    try:
        events = await oc.get_scheduled_events()
        agent_cache: dict[str, object] = {}

        for event in events:
            agent_state = agent_cache.get(event.agent_id)
            if agent_state is None and event.agent_id not in agent_cache:
                agent_state = await oc.get_agent_state(event.agent_id)
                agent_cache[event.agent_id] = agent_state

            if agent_state is None:
                logger.warning("No agent state for %s, skipping", event.agent_id)
                continue

            if not _needs_backup(event, agent_state.is_healthy, agent_state.is_offline):
                continue

            instance = _get_or_create_instance(
                db, event.agent_id, event.agent_id, agent_state.rtsp_source
            )
            instance.mediapackage_id = event.mediapackage_id

            if instance.status != InstanceStatus.running:
                logger.info("Starting backup instance for %s (event %s)", event.agent_id, event.mediapackage_id)
                try:
                    instance.status = InstanceStatus.starting
                    db.commit()
                    _provision_and_start(instance)
                    instance.status = InstanceStatus.running
                    instance.last_error = None
                except Exception as exc:  # noqa: BLE001 - surfaced via last_error
                    logger.exception("Failed to start backup for %s", event.agent_id)
                    instance.status = InstanceStatus.error
                    instance.last_error = str(exc)
                db.commit()

        # Tear down instances whose agent has recovered and has no near-term event.
        active_agent_ids = {e.agent_id for e in events}
        running_instances = db.query(Instance).filter_by(status=InstanceStatus.running).all()
        for instance in running_instances:
            if instance.agent_id in active_agent_ids:
                continue
            logger.info("Stopping backup instance for %s (no longer needed)", instance.agent_id)
            try:
                instance.status = InstanceStatus.stopping
                db.commit()
                compose_manager.down(instance)
                instance.status = InstanceStatus.stopped
                instance.mediapackage_id = None
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed to stop backup for %s", instance.agent_id)
                instance.status = InstanceStatus.error
                instance.last_error = str(exc)
            db.commit()
    finally:
        db.close()


async def run_forever() -> None:
    oc = OpencastClient()
    try:
        while True:
            try:
                await run_once(oc)
            except Exception:  # noqa: BLE001 - keep the loop alive
                logger.exception("Decision engine iteration failed")
            await asyncio.sleep(settings.poll_interval_seconds)
    finally:
        await oc.aclose()
