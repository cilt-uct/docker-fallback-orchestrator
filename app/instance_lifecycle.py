"""Creating, provisioning and inspecting a backup instance.

Shared by the HTTP API and the decision engine, so an instance is built and
started the same way whichever of them asks for it.
"""

import logging
import os
import threading
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy.orm import Session

from app import compose_manager, pyca_config
from app.config import safe_name, settings
from app.models import Instance, InstanceStatus
from app.port_allocator import allocate_port

logger = logging.getLogger(__name__)


class InstanceNotStartableError(ValueError):
    """The instance is missing something it needs before a start can even be attempted."""


class InstanceBusyError(RuntimeError):
    """Another start/stop/restart/delete of this instance is already in progress."""


# One lock per instance, shared by the API's worker threads and the decision engine (the
# controller is a single process), so the two never act on the same backup at once.
_instance_locks: defaultdict[int, threading.Lock] = defaultdict(threading.Lock)
_instance_locks_guard = threading.Lock()


@contextmanager
def instance_lock(instance_id: int):
    """Hold the instance's lock for the block, or raise InstanceBusyError at once if it's taken."""
    with _instance_locks_guard:
        lock = _instance_locks[instance_id]
    if not lock.acquire(blocking=False):
        raise InstanceBusyError("This backup is already being started or stopped - try again shortly")
    try:
        yield
    finally:
        lock.release()


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
        # Venues the engine creates are named after Opencast agent IDs, which can contain
        # anything; safe_name keeps the Docker names and folders valid.
        compose_project=f"pyca-{safe_name(venue)}",
        # Informational only - the folder is always derived via settings.instance_dir().
        config_dir=str(settings.instance_dir(venue)),
        status=InstanceStatus.stopped,
    )


def ensure_ui_credentials(instance: Instance) -> None:
    """Give the instance its PyCA UI login if it has none. Existing credentials are kept, not
    rotated on restart: operators log in with them and the decision engine uses them to check
    the backup's PyCA before teardown. Callers should commit before starting Docker, so a
    crash mid-start can't leave a running PyCA whose password the database doesn't know."""
    if not instance.ui_username or not instance.ui_password:
        instance.ui_username, instance.ui_password = pyca_config.generate_ui_credentials()


def provision(instance: Instance) -> None:
    """Write the instance's pyca.conf and compose file, and prepare its recordings folder."""
    if not instance.rtsp_source:
        raise InstanceNotStartableError(
            f"No RTSP source known for agent '{instance.agent_id}' - refusing to start "
            "a backup that could never capture anything"
        )
    ensure_ui_credentials(instance)
    prepare_recordings_dir(instance)
    pyca_config.write_pyca_conf(instance, instance.ui_username, instance.ui_password)
    compose_manager.render_compose_file(instance)


def provision_and_start(instance: Instance) -> None:
    provision(instance)
    compose_manager.up(instance)


def provision_and_restart(instance: Instance) -> None:
    """Restart with freshly written config, so e.g. a changed camera URL takes effect."""
    provision(instance)
    compose_manager.recreate(instance)


def recordings_dir(instance: Instance) -> Path:
    return settings.venue_recordings_dir(instance.venue)


def prepare_recordings_dir(instance: Instance) -> Path:
    """Create the venue's recordings folder before PyCA starts, writable by PyCA.

    Left to Docker, a missing bind-mount source is created owned by root, which PyCA
    (running as PYCA_UID) can't write to. When the controller runs as root - as it does
    in its container - the folder is handed to that user; otherwise it's created as the
    controller's own user, which must then be able to grant PyCA access itself.
    """
    path = recordings_dir(instance)
    path.mkdir(parents=True, exist_ok=True)
    if os.geteuid() == 0:
        try:
            os.chown(path, settings.pyca_uid, settings.pyca_uid)
        except OSError as exc:
            logger.warning("Could not hand %s to PyCA's user %s: %s", path, settings.pyca_uid, exc)
    return path


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
