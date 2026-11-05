"""Audit trail: who did what to which instance, and how it turned out.

Events are added to the caller's session rather than committed here, so an
audit row lands in the same transaction as the state change it describes.
Each event is also emitted on the "audit" logger, so a copy exists outside
MySQL.
"""

import json
import logging
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditActorType, AuditEvent, AuditOutcome, Instance

logger = logging.getLogger(__name__)

INSTANCE_CREATE = "instance.create"
INSTANCE_START = "instance.start"
INSTANCE_STOP = "instance.stop"
INSTANCE_RESTART = "instance.restart"
INSTANCE_DELETE = "instance.delete"
INSTANCE_RTSP_SOURCE_CHANGED = "instance.rtsp_source_changed"
INSTANCE_TEARDOWN_DEFERRED = "instance.teardown_deferred"


@dataclass(frozen=True)
class Actor:
    type: AuditActorType
    name: str | None = None
    source_ip: str | None = None


SYSTEM = Actor(type=AuditActorType.system, name="system")


def operator_actor(request: Request) -> Actor:
    """FastAPI dependency. The API has no auth yet, so an operator has no name."""
    return Actor(
        type=AuditActorType.operator,
        source_ip=request.client.host if request.client else None,
    )


def redact_url(url: str | None) -> str | None:
    """Strip userinfo from a URL - RTSP sources often embed camera credentials."""
    if not url:
        return url
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    return urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[1]))


def mediapackages_since_last_stop(db: Session, instance_id: int) -> list[str]:
    """Mediapackage IDs of the events this instance was successfully started for since it
    was last stopped, oldest first.

    An instance doesn't store "its" mediapackage: one running PyCA can record several
    events, so start/stop audit events carry the IDs instead and this reads them back.
    """
    last_stop_id = db.scalar(
        select(AuditEvent.id)
        .where(
            AuditEvent.instance_id == instance_id,
            AuditEvent.action == INSTANCE_STOP,
            AuditEvent.outcome == AuditOutcome.success,
        )
        .order_by(AuditEvent.id.desc())
        .limit(1)
    )
    starts = db.scalars(
        select(AuditEvent)
        .where(
            AuditEvent.instance_id == instance_id,
            AuditEvent.action == INSTANCE_START,
            AuditEvent.outcome == AuditOutcome.success,
            AuditEvent.id > (last_stop_id or 0),
        )
        .order_by(AuditEvent.id)
    )
    ids = [(event.detail or {}).get("mediapackage_id") for event in starts]
    return list(dict.fromkeys(i for i in ids if i))


def record(
    db: Session,
    *,
    actor: Actor,
    action: str,
    outcome: AuditOutcome = AuditOutcome.success,
    instance: Instance | None = None,
    venue: str | None = None,
    agent_id: str | None = None,
    detail: dict | None = None,
) -> AuditEvent:
    """Add an audit event to the session. The caller commits."""
    event = AuditEvent(
        actor_type=actor.type,
        actor=actor.name,
        source_ip=actor.source_ip,
        action=action,
        outcome=outcome,
        instance_id=instance.id if instance else None,
        venue=instance.venue if instance else venue,
        agent_id=instance.agent_id if instance else agent_id,
        detail=detail or None,
    )
    db.add(event)
    logger.info(
        json.dumps(
            {
                "actor_type": event.actor_type.value,
                "actor": event.actor,
                "source_ip": event.source_ip,
                "action": event.action,
                "outcome": event.outcome.value,
                "instance_id": event.instance_id,
                "venue": event.venue,
                "agent_id": event.agent_id,
                "detail": event.detail,
            }
        )
    )
    return event
