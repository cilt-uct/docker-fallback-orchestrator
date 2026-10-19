"""Creating, provisioning and inspecting a backup instance.

Shared by the HTTP API and the decision engine, so an instance is built and
started the same way whichever of them asks for it.
"""

from pathlib import Path

from sqlalchemy.orm import Session

from app import compose_manager, pyca_config
from app.config import settings
from app.models import Instance, InstanceStatus
from app.port_allocator import allocate_port


class InstanceNotStartableError(ValueError):
    """The instance is missing something it needs before a start can even be attempted."""


def build_instance(db: Session, venue: str, agent_id: str, rtsp_source: str | None) -> Instance:
    """A new, stopped instance with its UI port and naming filled in; not yet added to the session.

    The one place the compose project and config directory are derived from the venue, so
    instances created from the API and by this engine can't drift apart.
    Raises NoPortsAvailableError if the UI port range is full.
    """
    return Instance(
        venue=venue,
        agent_id=agent_id,
        rtsp_source=rtsp_source,
        ui_port=allocate_port(db),
        compose_project=f"pyca-{venue}",
        config_dir=str(settings.instances_dir / venue),
        status=InstanceStatus.stopped,
    )


def provision_and_start(instance: Instance) -> None:
    if not instance.rtsp_source:
        raise InstanceNotStartableError(
            f"No RTSP source known for agent '{instance.agent_id}' - refusing to start "
            "a backup that could never capture anything"
        )
    # Reuse existing UI credentials instead of rotating them on each restart.
    if not instance.ui_username or not instance.ui_password:
        instance.ui_username, instance.ui_password = pyca_config.generate_ui_credentials()
    pyca_config.write_pyca_conf(instance, instance.ui_username, instance.ui_password)
    compose_manager.render_compose_file(instance)
    compose_manager.up(instance)


def recordings_dir(instance: Instance) -> Path:
    return Path(instance.config_dir) / "data" / "recordings"


def dir_size_bytes(path: Path) -> int:
    """Total size of the files under path; 0 if it doesn't exist."""
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
