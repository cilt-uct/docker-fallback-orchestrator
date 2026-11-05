import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import audit, recordings
from app.audit import Actor, operator_actor
from app.config import settings
from app.constants import (
    HTTP_204_NO_CONTENT,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_500_INTERNAL_SERVER_ERROR,
)
from app.database import get_db
from app.schemas import RecordingRead, RetentionUpdate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/recordings", tags=["recordings"])


def _find_or_404(db: Session, venue: str, name: str) -> recordings.Recording:
    recording = recordings.find_recording(db, venue, name)
    if recording is None:
        raise HTTPException(HTTP_404_NOT_FOUND, "Recording not found")
    return recording


def _read(recording: recordings.Recording) -> RecordingRead:
    return RecordingRead(
        venue=recording.venue,
        name=recording.name,
        mediapackage_id=recording.mediapackage_id,
        started_at=recording.started_at,
        size_bytes=recording.size_bytes,
        pyca_status=recording.pyca_status,
        in_progress=recording.in_progress,
        retention_days=recording.retention_days,
        retention_overridden=recording.retention_overridden,
        delete_after=recording.delete_after,
    )


@router.get("", response_model=list[RecordingRead])
def list_recordings(venue: str | None = None, db: Session = Depends(get_db)):
    """Every backup recording on disk, newest first."""
    return [_read(r) for r in recordings.list_recordings(db, venue)]


@router.get("/settings")
def recording_settings():
    return {
        "default_retention_days": settings.recording_retention_days,
        "cleanup_interval_minutes": settings.recording_cleanup_interval_minutes,
    }


@router.put("/{venue}/{name}/retention", response_model=RecordingRead)
def set_retention(
    venue: str,
    name: str,
    payload: RetentionUpdate,
    db: Session = Depends(get_db),
    actor: Actor = Depends(operator_actor),
):
    """Change how many days this recording is kept, counted from its start."""
    recording = _find_or_404(db, venue, name)
    previous = recording.retention_days if recording.retention_overridden else None
    recordings.set_retention(db, recording, payload.retention_days)
    audit.record(
        db,
        actor=actor,
        action=audit.RECORDING_RETENTION_CHANGED,
        venue=venue,
        detail={
            "recording": name,
            "mediapackage_id": recording.mediapackage_id,
            "from": previous if previous is not None else f"default ({settings.recording_retention_days})",
            "to": payload.retention_days,
        },
    )
    db.commit()
    return _read(recordings.find_recording(db, venue, name))


@router.delete("/{venue}/{name}", status_code=HTTP_204_NO_CONTENT)
def delete_recording(
    venue: str,
    name: str,
    db: Session = Depends(get_db),
    actor: Actor = Depends(operator_actor),
):
    """Delete this recording now, ingested or not. Refused while PyCA is writing or uploading it."""
    recording = _find_or_404(db, venue, name)
    try:
        recordings.delete_recording(db, recording, actor)
    except recordings.RecordingInProgressError as exc:
        raise HTTPException(HTTP_409_CONFLICT, str(exc)) from exc
    except OSError as exc:
        logger.error("Could not delete recording %s/%s: %s", venue, name, exc)
        raise HTTPException(HTTP_500_INTERNAL_SERVER_ERROR, "Could not delete the recording's files") from exc
