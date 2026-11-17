from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models import AuditActorType, AuditOutcome, InstanceStatus


class InstanceCreate(BaseModel):
    # The venue is always the agent ID - the decision engine finds a venue's backup by its
    # agent ID, so a venue named otherwise would get a second backup for the same agent.
    # Docker and folder names come from safe_name(), so any agent ID works.
    agent_id: str = Field(min_length=1, max_length=128, pattern=r"^\S(.*\S)?$")
    rtsp_source: str | None = Field(default=None, max_length=512)
    # Accepted for older clients, but only if it's the agent ID.
    venue: str | None = None

    @model_validator(mode="after")
    def _venue_is_the_agent_id(self) -> "InstanceCreate":
        if self.venue is not None and self.venue != self.agent_id:
            raise ValueError("venue must be the same as agent_id (or left out)")
        self.venue = self.agent_id
        return self


class InstanceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    venue: str
    agent_id: str
    ui_port: int
    compose_project: str
    status: InstanceStatus
    rtsp_source: str | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    # Started by hand, so the decision engine won't tear it down (see audit.started_by_operator).
    started_by_operator: bool = False
    # The venue as used for Docker names and folders - e.g. its recordings folder.
    safe_name: str


class InstanceRuntimeStatus(BaseModel):
    """Live status pulled from Docker, layered on top of the DB record."""

    instance: InstanceRead
    containers: list[dict]
    recordings_bytes: int | None = None


class RecordingRead(BaseModel):
    venue: str
    name: str
    mediapackage_id: str | None
    started_at: datetime
    size_bytes: int
    pyca_status: str
    in_progress: bool
    retention_days: int
    retention_overridden: bool
    delete_after: datetime


class RetentionUpdate(BaseModel):
    retention_days: int = Field(ge=1, le=3650)


class AuditSummary(BaseModel):
    """Counts over the last `days` - the Audit log page's summary tiles."""

    days: int
    since: datetime
    failures: int
    backups_started: int
    backup_venues: int
    manual_actions: int
    recordings_deleted: int
    bytes_freed: int


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
