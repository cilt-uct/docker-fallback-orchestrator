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

from app import compose_manager, pyca_config, recordings
from app.config import safe_name, settings
from app.models import Instance, InstanceStatus
from app.port_allocator import allocate_port, load_port_map

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
        ui_port=allocate_port(db, venue),
        # Venues the engine creates are named after Opencast agent IDs, which can contain
        # anything; safe_name keeps the Docker names and folders valid.
        compose_project=f"pyca-{safe_name(venue)}",
        # Informational only - the folder is always derived via settings.instance_dir().
        config_dir=str(settings.instance_dir(venue)),
        status=InstanceStatus.stopped,
    )


def ensure_ui_credentials(instance: Instance) -> None:
    """Give the instance the PyCA UI login from settings (PYCA_UI_USERNAME/PASSWORD).

    Stored on the instance because the decision engine uses it to check the backup's PyCA
    before teardown, and a running PyCA keeps the login it was started with: a changed
    setting takes effect at the next start or restart. Callers should commit before starting
    Docker, so a crash mid-start can't leave a running PyCA whose login the database doesn't
    know."""
    instance.ui_username, instance.ui_password = pyca_config.ui_credentials()


def apply_port_map(db: Session, instance: Instance) -> None:
    """Move the instance to its mapped port, or off a port mapped to another venue.

    Called before each start or restart, so an edited port map takes effect then (a running
    PyCA keeps its port until restarted). Callers commit before starting Docker, so the
    database always knows the port PyCA is on. If another instance holds the mapped port,
    the instance keeps its current one: a backup that records matters more than its port.
    """
    port_map = load_port_map()
    port = port_map.get(instance.venue)
    if port is None:
        if instance.ui_port not in port_map.values():
            return
        port = allocate_port(db, instance.venue)  # reserved for another venue - move off it
    if port == instance.ui_port:
        return
    holder = db.query(Instance).filter(Instance.ui_port == port, Instance.id != instance.id).first()
    if holder:
        logger.warning(
            "Port %s is mapped to %s but %s is using it - keeping port %s until %s moves",
            port,
            instance.venue,
            holder.venue,
            instance.ui_port,
            holder.venue,
        )
        return
    logger.info("Moving %s's PyCA page from port %s to %s", instance.venue, instance.ui_port, port)
    instance.ui_port = port


def provision(instance: Instance) -> None:
    """Write the instance's pyca.conf and compose file, and prepare its recordings folder."""
    if not instance.rtsp_source:
        raise InstanceNotStartableError(
            f"No RTSP source known for agent '{instance.agent_id}' - refusing to start "
            "a backup that could never capture anything"
        )
    ensure_ui_credentials(instance)
    prepare_data_dir(instance)
    prepare_recordings_dir(instance)
    pyca_config.write_pyca_conf(instance, instance.ui_username, instance.ui_password)
    compose_manager.render_compose_file(instance)


def repair_interrupted_events(instance: Instance) -> list[dict]:
    """Fix up events PyCA was stopped in the middle of (see recordings.repair_interrupted),
    unless the instance's PyCA is running - then its database isn't ours to change."""
    if not (settings.instance_dir(instance.venue) / "data" / "pyca.db").is_file():
        return []
    try:
        if compose_manager.has_running_containers(instance):
            return []
    except compose_manager.DockerUnavailableError as exc:
        logger.warning(
            "Not repairing %s's PyCA database - can't tell if it's running: %s", instance.venue, exc
        )
        return []
    return recordings.repair_interrupted(instance.venue)


def provision_and_start(instance: Instance) -> list[dict]:
    """Start the instance; returns any interrupted events repaired first, for the audit log."""
    provision(instance)
    repaired = repair_interrupted_events(instance)
    compose_manager.up(instance)
    return repaired


def provision_and_restart(instance: Instance) -> list[dict]:
    """Restart with freshly written config, so e.g. a changed camera URL takes effect.

    A full stop and start rather than a recreate in place, so events interrupted by the
    stop can be repaired while PyCA is down."""
    compose_manager.down(instance)
    return provision_and_start(instance)


def recordings_dir(instance: Instance) -> Path:
    return settings.venue_recordings_dir(instance.venue)


def _make_pyca_dir(path: Path) -> Path:
    """Create a folder PyCA bind-mounts, writable by PyCA.

    Left to Docker, a missing bind-mount source is created owned by root, which PyCA
    (running as PYCA_UID) can't write to. When the controller runs as root - as it does
    in its container - the folder is handed to that user; otherwise it's created as the
    controller's own user, which must then be able to grant PyCA access itself.
    """
    path.mkdir(parents=True, exist_ok=True)
    if os.geteuid() == 0:
        try:
            os.chown(path, settings.pyca_uid, settings.pyca_uid)
        except OSError as exc:
            logger.warning("Could not hand %s to PyCA's user %s: %s", path, settings.pyca_uid, exc)
    return path


def prepare_data_dir(instance: Instance) -> Path:
    """Create the instance's data folder (PyCA's sqlite database) before PyCA starts."""
    return _make_pyca_dir(settings.instance_dir(instance.venue) / "data")


def prepare_recordings_dir(instance: Instance) -> Path:
    """Create the venue's recordings folder before PyCA starts."""
    return _make_pyca_dir(recordings_dir(instance))


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
