from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models import AuditActorType, AuditOutcome, InstanceStatus


class InstanceCreate(BaseModel):
    # The venue names the compose project and the config directory, so it has to be
    # valid for both - Docker rejects anything else only later, at start time.
    venue: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    agent_id: str = Field(min_length=1, max_length=128)
    rtsp_source: str | None = Field(default=None, max_length=512)


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
