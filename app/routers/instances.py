from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import compose_manager, pyca_config
from app.database import get_db
from app.models import Instance, InstanceStatus
from app.port_allocator import allocate_port
from app.config import settings
from app.schemas import InstanceCreate, InstanceRead, InstanceRuntimeStatus

router = APIRouter(prefix="/instances", tags=["instances"])


@router.get("", response_model=list[InstanceRead])
def list_instances(db: Session = Depends(get_db)):
    return db.query(Instance).all()


@router.post("", response_model=InstanceRead, status_code=201)
def create_instance(payload: InstanceCreate, db: Session = Depends(get_db)):
    if db.query(Instance).filter_by(venue=payload.venue).first():
        raise HTTPException(409, f"Instance for venue '{payload.venue}' already exists")

    instance = Instance(
        venue=payload.venue,
        agent_id=payload.agent_id,
        rtsp_source=payload.rtsp_source,
        ui_port=allocate_port(db),
        compose_project=f"pyca-{payload.venue}",
        config_dir=str(settings.instances_dir / payload.venue),
        status=InstanceStatus.stopped,
    )
    db.add(instance)
    db.commit()
    db.refresh(instance)
    return instance


def _get_instance_or_404(db: Session, instance_id: int) -> Instance:
    instance = db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(404, "Instance not found")
    return instance


@router.get("/{instance_id}", response_model=InstanceRuntimeStatus)
def get_instance(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_instance_or_404(db, instance_id)
    return InstanceRuntimeStatus(
        instance=instance,
        containers=compose_manager.container_statuses(instance),
    )


@router.post("/{instance_id}/start", response_model=InstanceRead)
def start_instance(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_instance_or_404(db, instance_id)
    try:
        ui_username, ui_password = pyca_config.generate_ui_credentials()
        pyca_config.write_pyca_conf(instance, ui_username, ui_password)
        compose_manager.render_compose_file(instance)
        compose_manager.up(instance)
        instance.status = InstanceStatus.running
        instance.last_error = None
    except Exception as exc:  # noqa: BLE001
        instance.status = InstanceStatus.error
        instance.last_error = str(exc)
        db.commit()
        raise HTTPException(500, f"Failed to start instance: {exc}") from exc
    db.commit()
    db.refresh(instance)
    return instance


@router.post("/{instance_id}/stop", response_model=InstanceRead)
def stop_instance(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_instance_or_404(db, instance_id)
    try:
        compose_manager.down(instance)
        instance.status = InstanceStatus.stopped
        instance.mediapackage_id = None
        instance.last_error = None
    except Exception as exc:  # noqa: BLE001
        instance.status = InstanceStatus.error
        instance.last_error = str(exc)
        db.commit()
        raise HTTPException(500, f"Failed to stop instance: {exc}") from exc
    db.commit()
    db.refresh(instance)
    return instance


@router.post("/{instance_id}/restart", response_model=InstanceRead)
def restart_instance(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_instance_or_404(db, instance_id)
    try:
        compose_manager.restart(instance)
        instance.last_error = None
    except Exception as exc:  # noqa: BLE001
        instance.status = InstanceStatus.error
        instance.last_error = str(exc)
        db.commit()
        raise HTTPException(500, f"Failed to restart instance: {exc}") from exc
    db.commit()
    db.refresh(instance)
    return instance


@router.delete("/{instance_id}", status_code=204)
def delete_instance(instance_id: int, db: Session = Depends(get_db)):
    instance = _get_instance_or_404(db, instance_id)
    if instance.status == InstanceStatus.running:
        raise HTTPException(409, "Stop the instance before deleting it")
    db.delete(instance)
    db.commit()
