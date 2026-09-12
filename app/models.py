import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


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
    mediapackage_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
