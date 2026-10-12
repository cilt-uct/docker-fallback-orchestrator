from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AuditActorType, AuditEvent, AuditOutcome
from app.schemas import AuditEventRead

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("", response_model=list[AuditEventRead])
def list_audit_events(
    instance_id: int | None = None,
    venue: str | None = None,
    action: str | None = None,
    actor_type: AuditActorType | None = None,
    outcome: AuditOutcome | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    before_id: int | None = None,
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """Newest first. Page backwards by passing the last row's id as before_id."""
    query = db.query(AuditEvent)
    if instance_id is not None:
        query = query.filter(AuditEvent.instance_id == instance_id)
    if venue is not None:
        query = query.filter(AuditEvent.venue == venue)
    if action is not None:
        query = query.filter(AuditEvent.action == action)
    if actor_type is not None:
        query = query.filter(AuditEvent.actor_type == actor_type)
    if outcome is not None:
        query = query.filter(AuditEvent.outcome == outcome)
    if since is not None:
        query = query.filter(AuditEvent.occurred_at >= since)
    if until is not None:
        query = query.filter(AuditEvent.occurred_at <= until)
    if before_id is not None:
        query = query.filter(AuditEvent.id < before_id)
    return query.order_by(AuditEvent.id.desc()).limit(limit).all()
