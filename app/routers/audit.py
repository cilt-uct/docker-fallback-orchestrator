from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import audit
from app.database import get_db
from app.models import AuditActorType, AuditEvent, AuditOutcome, utcnow
from app.schemas import AuditEventRead, AuditSummary

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("/summary", response_model=AuditSummary)
def audit_summary(
    days: int = Query(7, ge=1, le=365),
    venue: str | None = None,
    db: Session = Depends(get_db),
):
    """Counts for the Audit log page's summary tiles, over the last `days` (optionally one venue)."""
    since = utcnow() - timedelta(days=days)
    window = db.query(AuditEvent).filter(AuditEvent.occurred_at >= since)
    if venue is not None:
        window = window.filter(AuditEvent.venue == venue)

    def count(*conditions) -> int:
        return window.filter(*conditions).count()

    backup_starts = window.filter(
        AuditEvent.action == audit.INSTANCE_START,
        AuditEvent.actor_type == AuditActorType.system,
        AuditEvent.outcome == AuditOutcome.success,
    )
    deleted = window.filter(AuditEvent.action == audit.RECORDING_DELETED).all()
    return AuditSummary(
        days=days,
        since=since,
        failures=count(AuditEvent.outcome == AuditOutcome.failure),
        backups_started=backup_starts.count(),
        backup_venues=backup_starts.with_entities(func.count(func.distinct(AuditEvent.venue))).scalar() or 0,
        manual_actions=count(AuditEvent.actor_type == AuditActorType.operator),
        recordings_deleted=len(deleted),
        # Sizes are in each event's JSON detail; summed here rather than in SQL, which would need
        # different JSON functions on MySQL and SQLite. Deletions are few per week.
        bytes_freed=sum(int((event.detail or {}).get("size_bytes") or 0) for event in deleted),
    )


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
    offset: int = Query(0, ge=0),
    *,
    response: Response,
    db: Session = Depends(get_db),
):
    """Newest first. Page with limit + offset (the dashboard does); the X-Total-Count header
    gives the number of events matching the filters. before_id also still works, for
    paging backwards from a known event."""
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
    response.headers["X-Total-Count"] = str(query.count())
    return query.order_by(AuditEvent.id.desc()).offset(offset).limit(limit).all()
