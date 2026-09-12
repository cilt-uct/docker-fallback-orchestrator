import shutil
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Instance, InstanceStatus

router = APIRouter(prefix="/monitoring", tags=["monitoring"])


def _dir_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/summary")
def summary(db: Session = Depends(get_db)):
    instances = db.query(Instance).all()
    by_status = {status.value: 0 for status in InstanceStatus}
    for instance in instances:
        by_status[instance.status.value] += 1

    recordings_root = Path(instances[0].config_dir).parent if instances else None
    disk_usage = shutil.disk_usage(recordings_root) if recordings_root and recordings_root.exists() else None

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
