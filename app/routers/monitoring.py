import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import decision_engine
from app.config import settings
from app.database import get_db
from app.models import Instance, InstanceStatus

logger = logging.getLogger("api.monitoring")

router = APIRouter(prefix="/monitoring", tags=["monitoring"])

# The engine is "stalled" once this many poll intervals pass without a successful poll.
ENGINE_STALE_POLLS = 3


def _dir_size_bytes(path: Path) -> int:
    # PyCA writes and removes files under here while we walk it, so any entry can
    # vanish (or be unreadable) between being listed and being stat'ed.
    total = 0
    try:
        for f in path.rglob("*"):
            try:
                if f.is_file():
                    total += f.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


def _disk_usage(path: Path | None):
    if path is None:
        return None
    try:
        return shutil.disk_usage(path)
    except OSError:
        return None


@router.get("/health")
def health():
    """Liveness: the process is up and serving. Checks nothing else - see /ready."""
    return {"status": "ok"}


@router.get("/ready")
def ready(request: Request, db: Session = Depends(get_db)):
    """Readiness: 200 only if the database answers and the decision engine is polling successfully."""
    checks = {}

    try:
        db.execute(text("SELECT 1"))
        checks["database"] = {"ok": True}
    except SQLAlchemyError as exc:
        logger.warning("Readiness: database check failed: %s", exc)
        checks["database"] = {"ok": False, "error": "database unavailable"}

    engine = decision_engine.status
    task = getattr(request.app.state, "engine_task", None)
    now = datetime.now(timezone.utc)
    stale_after = ENGINE_STALE_POLLS * settings.poll_interval_seconds
    if task is not None and task.done():
        engine_ok, engine_error = False, "decision engine is not running"
    elif engine.last_success_at is None:
        # Not ready until the first poll has succeeded; a failure so far says why.
        engine_ok, engine_error = False, engine.last_error or "no poll has completed yet"
    elif (now - engine.last_success_at).total_seconds() > stale_after:
        engine_ok, engine_error = False, engine.last_error or "no successful poll recently"
    else:
        engine_ok, engine_error = True, None
    checks["decision_engine"] = {
        "ok": engine_ok,
        "error": engine_error,
        "last_success_at": engine.last_success_at.isoformat() if engine.last_success_at else None,
        "consecutive_failures": engine.consecutive_failures,
    }

    all_ok = all(check["ok"] for check in checks.values())
    return JSONResponse(
        status_code=200 if all_ok else 503,
        content={"status": "ok" if all_ok else "unavailable", "checks": checks},
    )


@router.get("/summary")
def summary(db: Session = Depends(get_db)):
    instances = db.query(Instance).all()
    by_status = {status.value: 0 for status in InstanceStatus}
    for instance in instances:
        by_status[instance.status.value] += 1

    disk_usage = _disk_usage(Path(instances[0].config_dir).parent if instances else None)

    return {
        "instance_count": len(instances),
        "by_status": by_status,
        "recordings_bytes_by_instance": {
            instance.venue: _dir_size_bytes(Path(instance.config_dir) / "data" / "recordings")
            for instance in instances
        },
        "disk_usage": (
            {"total": disk_usage.total, "used": disk_usage.used, "free": disk_usage.free}
            if disk_usage
            else None
        ),
    }
