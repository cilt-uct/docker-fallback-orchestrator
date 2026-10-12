from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models import AuditActorType, AuditOutcome, InstanceStatus


class InstanceCreate(BaseModel):
    venue: str
    agent_id: str
    rtsp_source: str | None = None


class InstanceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    venue: str
    agent_id: str
    ui_port: int
    compose_project: str
    status: InstanceStatus
    rtsp_source: str | None
    mediapackage_id: str | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class InstanceRuntimeStatus(BaseModel):
    """Live status pulled from Docker, layered on top of the DB record."""

    instance: InstanceRead
    containers: list[dict]
    recordings_bytes: int | None = None


class AuditEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    occurred_at: datetime
    actor_type: AuditActorType
    actor: str | None
    source_ip: str | None
    action: str
    outcome: AuditOutcome
    instance_id: int | None
    venue: str | None
    agent_id: str | None
    detail: dict | None
