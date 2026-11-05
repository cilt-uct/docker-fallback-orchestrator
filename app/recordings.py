"""Backup recordings on disk: finding them, their status, and the retention clean-up.

Recordings aren't tracked in the database. PyCA writes each one to its own folder,
`<RECORDINGS_DIR>/<venue>/recording-<start epoch>-<mediapackage id>/`, and records its
status in that instance's own SQLite database (`<INSTANCES_DIR>/<venue>/data/pyca.db`,
table `recorded_event`, keyed by the same mediapackage ID and start time). This
module reads both, read-only, so it works whether or not the backup is running and
for venues whose instance row has since been deleted.

Clean-up deletes a recording once RECORDING_RETENTION_DAYS (or that recording's
override, see models.RecordingRetention) have passed since it started - whether or
not it was ingested. The one exception is a recording PyCA is still writing or
uploading, which is left for a later run.
"""

import asyncio
import logging
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app import audit
from app.config import settings
from app.database import SessionLocal
from app.models import RecordingRetention, utcnow

logger = logging.getLogger(__name__)

RECORDING_DIR_PATTERN = re.compile(r"^recording-(\d+)-([A-Za-z0-9._-]+)$")
VENUE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

# PyCA's recorded_event.status values (pyca/db.py, class Status).
PYCA_STATUS = {
    1: "upcoming",
    2: "recording",
    3: "failed recording",
    4: "finished recording",
    5: "uploading",
    6: "failed uploading",
    7: "finished uploading",
    8: "partial recording",
    9: "paused after recording",
}
# Being written or sent right now - deleting would cut it off mid-way.
IN_PROGRESS_STATUSES = {"recording", "uploading"}


@dataclass
class Recording:
    venue: str
    name: str
    path: Path
    mediapackage_id: str | None
    started_at: datetime  # naive UTC, like the database
    size_bytes: int
    pyca_status: str  # one of PYCA_STATUS's values, or "unknown"
    retention_days: int
    retention_overridden: bool

    @property
    def delete_after(self) -> datetime:
        return self.started_at + timedelta(days=self.retention_days)

    @property
    def in_progress(self) -> bool:
        return self.pyca_status in IN_PROGRESS_STATUSES


def _venue_dirs() -> list[Path]:
    """Each venue's recordings folder under RECORDINGS_DIR."""
    root = settings.recordings_dir.resolve()
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and VENUE_PATTERN.match(p.name))


def _pyca_statuses(venue: str) -> dict[tuple[str, int], str]:
    """(mediapackage id, start epoch) -> status, read from the instance's own PyCA database."""
    db_path = settings.instance_dir(venue) / "data" / "pyca.db"
    if not db_path.is_file():
        return {}
    try:
        # Read-only: PyCA may be writing to it while we read.
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5) as conn:
            rows = conn.execute("SELECT uid, start, status FROM recorded_event").fetchall()
    except sqlite3.Error as exc:
        logger.warning("Could not read PyCA's database for %s: %s", venue, exc)
        return {}
    return {(uid, int(start)): PYCA_STATUS.get(status, "unknown") for uid, start, status in rows}


def _dir_size(path: Path) -> int:
    total = 0
    for f in path.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            continue
    return total


def list_recordings(db: Session, venue: str | None = None) -> list[Recording]:
    """Every recording on disk (optionally one venue's), newest first."""
    overrides = {(r.venue, r.name): r.retention_days for r in db.query(RecordingRetention)}
    recordings = []
    for venue_dir in _venue_dirs():
        if venue is not None and venue_dir.name != venue:
            continue
        statuses = _pyca_statuses(venue_dir.name)
        for path in venue_dir.iterdir():
            match = RECORDING_DIR_PATTERN.match(path.name)
            if not path.is_dir() or not match:
                continue
            start_epoch, mediapackage_id = int(match.group(1)), match.group(2)
            override = overrides.get((venue_dir.name, path.name))
            recordings.append(
                Recording(
                    venue=venue_dir.name,
                    name=path.name,
                    path=path,
                    mediapackage_id=mediapackage_id,
                    started_at=datetime.fromtimestamp(start_epoch, UTC).replace(tzinfo=None),
                    size_bytes=_dir_size(path),
                    pyca_status=statuses.get((mediapackage_id, start_epoch), "unknown"),
                    retention_days=override if override is not None else settings.recording_retention_days,
                    retention_overridden=override is not None,
                )
            )
    return sorted(recordings, key=lambda r: r.started_at, reverse=True)


def find_recording(db: Session, venue: str, name: str) -> Recording | None:
    if not VENUE_PATTERN.match(venue) or not RECORDING_DIR_PATTERN.match(name):
        return None  # also rules out any path traversal
    return next((r for r in list_recordings(db, venue) if r.name == name), None)


def set_retention(db: Session, recording: Recording, retention_days: int) -> None:
    """Set how many days this recording is kept, from its start. Caller commits."""
    row = db.get(RecordingRetention, (recording.venue, recording.name))
    if row is None:
        db.add(RecordingRetention(venue=recording.venue, name=recording.name, retention_days=retention_days))
    else:
        row.retention_days = retention_days


def _forget_retention(db: Session, recording: Recording) -> None:
    """Drop a deleted recording's setting, so it doesn't outlive the recording. Caller commits."""
    row = db.get(RecordingRetention, (recording.venue, recording.name))
    if row is not None:
        db.delete(row)


class RecordingInProgressError(RuntimeError):
    """PyCA is still writing or uploading this recording, so it can't be deleted yet."""


def delete_recording(db: Session, recording: Recording, actor: audit.Actor) -> None:
    """Delete one recording's folder, drop its retention setting, and audit it. Commits.

    Raises RecordingInProgressError while PyCA is writing or uploading it, and OSError if
    the folder can't be removed (nothing is recorded then).
    """
    if recording.in_progress:
        raise RecordingInProgressError(
            f"{recording.venue}/{recording.name} is still {recording.pyca_status}"
            " - try again once it finishes"
        )
    shutil.rmtree(recording.path)
    _forget_retention(db, recording)
    audit.record(
        db,
        actor=actor,
        action=audit.RECORDING_DELETED,
        venue=recording.venue,
        detail={
            "recording": recording.name,
            "mediapackage_id": recording.mediapackage_id,
            "pyca_status": recording.pyca_status,
            "started_at": recording.started_at.isoformat(),
            "retention_days": recording.retention_days,
            "size_bytes": recording.size_bytes,
            # "retention" when the clean-up task deleted it, "manual" from the Recordings page.
            "trigger": "retention" if actor.type == audit.SYSTEM.type else "manual",
        },
    )
    db.commit()


def clean_up(db: Session, now: datetime | None = None) -> list[Recording]:
    """Delete every recording past its retention, except ones in progress. Returns those deleted."""
    now = now or utcnow()
    deleted = []
    for recording in list_recordings(db):
        if recording.delete_after > now:
            continue
        try:
            delete_recording(db, recording, audit.SYSTEM)
        except RecordingInProgressError:
            logger.info(
                "Recording %s/%s is past retention but still %s - skipping for now",
                recording.venue,
                recording.name,
                recording.pyca_status,
            )
            continue
        except OSError as exc:
            logger.error("Could not delete recording %s/%s: %s", recording.venue, recording.name, exc)
            continue
        logger.info(
            "Deleted recording %s/%s (%s, past %d-day retention)",
            recording.venue,
            recording.name,
            recording.pyca_status,
            recording.retention_days,
        )
        deleted.append(recording)
    return deleted


def _clean_up_once() -> None:
    db = SessionLocal()
    try:
        clean_up(db)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def run_cleanup_forever() -> None:
    """Background task: clean up every RECORDING_CLEANUP_INTERVAL_MINUTES. Never dies on an error."""
    while True:
        try:
            # Walking and deleting folders blocks - keep it off the event loop the API runs on.
            await asyncio.to_thread(_clean_up_once)
        except Exception:  # noqa: BLE001 - keep the loop alive
            logger.exception("Recording clean-up failed")
        await asyncio.sleep(settings.recording_cleanup_interval_minutes * 60)
