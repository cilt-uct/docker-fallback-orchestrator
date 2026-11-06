import logging
from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit, compose_manager
from app.audit import Actor, operator_actor
from app.constants import (
    HTTP_201_CREATED,
    HTTP_204_NO_CONTENT,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_500_INTERNAL_SERVER_ERROR,
    HTTP_502_BAD_GATEWAY,
    HTTP_503_SERVICE_UNAVAILABLE,
    HTTP_504_GATEWAY_TIMEOUT,
)
from app.database import get_db
from app.errors import new_error_id
from app.instance_lifecycle import (
    InstanceBusyError,
    InstanceNotStartableError,
    build_instance,
    dir_size_bytes,
    ensure_ui_credentials,
    instance_lock,
    provision_and_restart,
    provision_and_start,
    recordings_dir,
)
from app.models import AuditOutcome, Instance, InstanceStatus
from app.port_allocator import NoPortsAvailableError
from app.schemas import InstanceCreate, InstanceRead, InstanceRuntimeStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/instances", tags=["instances"])


def _action_failed(
    db: Session, actor: Actor, action: str, verb: str, instance: Instance, exc: Exception
) -> HTTPException:
    """Record a failed start/stop/restart on the instance and in the audit log, and build the response.

    Failures we expect keep their message, since it's what the operator needs to act on
    (e.g. docker compose's stderr). Anything else is logged with a reference and not echoed.
    """
    if isinstance(exc, InstanceNotStartableError):
        status_code, detail = HTTP_409_CONFLICT, str(exc)
    elif isinstance(exc, compose_manager.ComposeTimeoutError):
        status_code, detail = HTTP_504_GATEWAY_TIMEOUT, str(exc)
    elif isinstance(exc, compose_manager.ComposeError):
        status_code, detail = HTTP_502_BAD_GATEWAY, str(exc)
    else:
        error_id = new_error_id()
        logger.error("Unexpected error [%s] trying to %s %s", error_id, verb, instance.venue, exc_info=exc)
        status_code, detail = (
            HTTP_500_INTERNAL_SERVER_ERROR,
            f"Failed to {verb} instance: internal error (ref {error_id})",
        )
    if status_code != HTTP_500_INTERNAL_SERVER_ERROR:
        logger.warning("Failed to %s %s: %s", verb, instance.venue, exc)

    instance.status = InstanceStatus.error
    instance.last_error = str(exc)
    audit.record(
        db,
        actor=actor,
        action=action,
        outcome=AuditOutcome.failure,
        instance=instance,
        detail={"error": str(exc)},
    )
    db.commit()
    return HTTPException(status_code, detail)


@router.get("", response_model=list[InstanceRead])
def list_instances(db: Session = Depends(get_db)):
    instances = db.query(Instance).all()
    for instance in instances:
        _mark_operator_hold(db, instance)
    return instances


@contextmanager
def _locked(instance: Instance):
    """Hold the instance's lock (shared with the decision engine), or answer 409 if it's taken."""
    try:
        with instance_lock(instance.id):
            yield
    except InstanceBusyError as exc:
        raise HTTPException(HTTP_409_CONFLICT, str(exc)) from exc


def _mark_operator_hold(db: Session, instance: Instance) -> Instance:
    """Expose whether the instance is held up by a manual start (InstanceRead.started_by_operator)."""
    instance.started_by_operator = instance.status == InstanceStatus.running and audit.started_by_operator(
        db, instance.id
    )
    return instance


def _create_rejected(
    db: Session, actor: Actor, payload: InstanceCreate, error: str, detail: str
) -> HTTPException:
    """Audit a create that didn't happen, and build its 409 response."""
    audit.record(
        db,
        actor=actor,
        action=audit.INSTANCE_CREATE,
        outcome=AuditOutcome.failure,
        venue=payload.venue,
        agent_id=payload.agent_id,
        detail={"error": error},
    )
    db.commit()
    return HTTPException(HTTP_409_CONFLICT, detail)


@router.post("", response_model=InstanceRead, status_code=HTTP_201_CREATED)
def create_instance(
    payload: InstanceCreate,
    db: Session = Depends(get_db),
    actor: Actor = Depends(operator_actor),
):
    if db.query(Instance).filter_by(venue=payload.venue).first():
        raise _create_rejected(
            db, actor, payload, "venue already exists", f"Instance for venue '{payload.venue}' already exists"
        )

    try:
        instance = build_instance(db, payload.venue, payload.agent_id, payload.rtsp_source)
    except NoPortsAvailableError as exc:
        raise _create_rejected(
            db, actor, payload, str(exc), f"{exc} - delete an unused instance or widen the range"
        ) from exc

    db.add(instance)
    try:
        db.flush()
    except IntegrityError as exc:
        # Lost a race with another create (or the decision engine) for this venue or port.
        db.rollback()
        raise _create_rejected(
            db,
            actor,
            payload,
            "created concurrently",
            f"Instance for venue '{payload.venue}' was created concurrently - refresh and retry",
        ) from exc
    audit.record(
        db,
        actor=actor,
        action=audit.INSTANCE_CREATE,
        instance=instance,
        detail={"rtsp_source": audit.redact_url(instance.rtsp_source), "ui_port": instance.ui_port},
    )
    db.commit()
    db.refresh(instance)
    return instance


def _get_instance_or_404(db: Session, instance_id: int) -> Instance:
    instance = db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(HTTP_404_NOT_FOUND, "Instance not found")
    return instance


@router.get("/{instance_id}", response_model=InstanceRuntimeStatus)
def get_instance(instance_id: int, db: Session = Depends(get_db)):
    instance = _mark_operator_hold(db, _get_instance_or_404(db, instance_id))
    try:
        containers = compose_manager.container_statuses(instance)
    except compose_manager.DockerUnavailableError as exc:
        logger.warning("%s", exc)
        raise HTTPException(
            HTTP_503_SERVICE_UNAVAILABLE, "Docker is unavailable, so container status can't be read"
        ) from exc
    return InstanceRuntimeStatus(
        instance=instance,
        containers=containers,
        recordings_bytes=dir_size_bytes(recordings_dir(instance)),
    )


@router.post("/{instance_id}/start", response_model=InstanceRead)
def start_instance(
    instance_id: int,
    db: Session = Depends(get_db),
    actor: Actor = Depends(operator_actor),
):
    instance = _get_instance_or_404(db, instance_id)
    with _locked(instance):
        # Saved before Docker is touched, so a crash mid-start can't leave a running PyCA
        # whose login the database doesn't know (see ensure_ui_credentials).
        instance.status = InstanceStatus.starting
        ensure_ui_credentials(instance)
        db.commit()
        try:
            provision_and_start(instance)
            instance.status = InstanceStatus.running
            instance.last_error = None
        except Exception as exc:  # noqa: BLE001 - classified in _action_failed
            raise _action_failed(db, actor, audit.INSTANCE_START, "start", instance, exc) from exc
        audit.record(db, actor=actor, action=audit.INSTANCE_START, instance=instance)
        db.commit()
    db.refresh(instance)
    return _mark_operator_hold(db, instance)


@router.post("/{instance_id}/stop", response_model=InstanceRead)
def stop_instance(
    instance_id: int,
    db: Session = Depends(get_db),
    actor: Actor = Depends(operator_actor),
):
    instance = _get_instance_or_404(db, instance_id)
    with _locked(instance):
        # Read before the stop is recorded, which resets the "since last stop" window.
        mediapackage_ids = audit.mediapackages_since_last_stop(db, instance.id)
        try:
            compose_manager.down(instance)
            instance.status = InstanceStatus.stopped
            instance.last_error = None
        except Exception as exc:  # noqa: BLE001 - classified in _action_failed
            raise _action_failed(db, actor, audit.INSTANCE_STOP, "stop", instance, exc) from exc
        audit.record(
            db,
            actor=actor,
            action=audit.INSTANCE_STOP,
            instance=instance,
            detail={"mediapackage_ids": mediapackage_ids},
        )
        db.commit()
    db.refresh(instance)
    return _mark_operator_hold(db, instance)


@router.post("/{instance_id}/restart", response_model=InstanceRead)
def restart_instance(
    instance_id: int,
    db: Session = Depends(get_db),
    actor: Actor = Depends(operator_actor),
):
    instance = _get_instance_or_404(db, instance_id)
    with _locked(instance):
        try:
            provision_and_restart(instance)
            instance.last_error = None
        except Exception as exc:  # noqa: BLE001 - classified in _action_failed
            raise _action_failed(db, actor, audit.INSTANCE_RESTART, "restart", instance, exc) from exc
        audit.record(db, actor=actor, action=audit.INSTANCE_RESTART, instance=instance)
        db.commit()
    db.refresh(instance)
    return _mark_operator_hold(db, instance)


@router.delete("/{instance_id}", status_code=HTTP_204_NO_CONTENT)
def delete_instance(
    instance_id: int,
    db: Session = Depends(get_db),
    actor: Actor = Depends(operator_actor),
):
    instance = _get_instance_or_404(db, instance_id)
    if instance.status == InstanceStatus.running:
        audit.record(
            db,
            actor=actor,
            action=audit.INSTANCE_DELETE,
            outcome=AuditOutcome.failure,
            instance=instance,
            detail={"error": "instance is running"},
        )
        db.commit()
        raise HTTPException(HTTP_409_CONFLICT, "Stop the instance before deleting it")
    with _locked(instance):
        # Not "running" doesn't mean no containers: a failed stop leaves an instance in
        # "error" with everything up. Remove them first, or they'd run on untracked.
        try:
            compose_manager.down(instance)
        except Exception as exc:  # noqa: BLE001 - classified in _action_failed
            raise _action_failed(db, actor, audit.INSTANCE_DELETE, "delete", instance, exc) from exc
        audit.record(db, actor=actor, action=audit.INSTANCE_DELETE, instance=instance)
        db.delete(instance)
        db.commit()
