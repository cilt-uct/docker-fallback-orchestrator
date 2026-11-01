import enum
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Enum, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def utcnow() -> datetime:
    """Current UTC time, naive. Columns store naive UTC (and the API returns it without
    an offset, which the dashboard reads as UTC); datetime.utcnow is deprecated."""
    return datetime.now(UTC).replace(tzinfo=None)


class InstanceStatus(str, enum.Enum):
    stopped = "stopped"
    starting = "starting"
    running = "running"
    stopping = "stopping"
    error = "error"


class Instance(Base):
    """A managed backup PyCA instance for one venue/capture agent."""

    __tablename__ = "instances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # The Opencast capture agent this instance backs up.
    venue: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    agent_id: Mapped[str] = mapped_column(String(128), nullable=False)

    ui_port: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    compose_project: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    config_dir: Mapped[str] = mapped_column(String(512), nullable=False)

    status: Mapped[InstanceStatus] = mapped_column(
        Enum(InstanceStatus), default=InstanceStatus.stopped, nullable=False
    )
    rtsp_source: Mapped[str | None] = mapped_column(String(512), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Not rotated on restart - see decision_engine.pyca_activity.
    ui_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ui_password: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AgentHealth(Base):
    """What the decision engine has observed about one capture agent's health over time.

    Opencast only reports an agent's *current* state, but the backup rules look back
    ("down at any point in the last 24 hours", "healthy for 24 hours"), so the engine
    records its own observations here. One row per agent ID.
    """

    __tablename__ = "agent_health"

    agent_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    last_checked_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # Last time the agent was seen offline or in an unhealthy state.
    last_unhealthy_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Start of the current unbroken run of healthy observations; None while unhealthy.
    healthy_since: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # End of the latest scheduled event that needed a backup.
    last_needed_event_end_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AuditActorType(str, enum.Enum):
    operator = "operator"
    system = "system"


class AuditOutcome(str, enum.Enum):
    success = "success"
    failure = "failure"


class AuditEvent(Base):
    """Append-only record of one state-changing action on an instance."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    actor_type: Mapped[AuditActorType] = mapped_column(Enum(AuditActorType), nullable=False)
    # "system" for the decision engine; null for operators until the API has auth.
    actor: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)

    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    outcome: Mapped[AuditOutcome] = mapped_column(Enum(AuditOutcome), nullable=False)

    # Snapshots, deliberately not foreign keys - history must outlive the instance row.
    instance_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    venue: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    agent_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
