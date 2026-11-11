"""Which host port each backup's PyCA web page is published on.

Venues listed in the port map file (PYCA_PORTS_FILE) always get their own port, so a
venue's PyCA page doesn't move when backups come and go. Any other venue gets the
lowest free port in the UI range that isn't reserved for a mapped venue.
"""

import logging

import yaml
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Instance

logger = logging.getLogger(__name__)


class NoPortsAvailableError(RuntimeError):
    pass


class PortMapError(ValueError):
    """The port map file can't be used as it stands."""


def load_port_map() -> dict[str, int]:
    """Venue -> port from the port map file; empty if there is no file.

    Read on every call, so an edit applies to the next backup that starts.
    """
    path = settings.pyca_ports_file
    if not path or not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise PortMapError(f"{path} isn't valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise PortMapError(f"{path} must map venue names to ports, e.g. 'lt1: 9001'")

    ports: dict[str, int] = {}
    venue_on_port: dict[int, str] = {}
    for venue, port in data.items():
        # bool is an int in Python; "yes: 9001" or "lt1: true" is a typo, not a port.
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise PortMapError(
                f"{path}: port for venue '{venue}' must be a number from 1 to 65535, not {port!r}"
            )
        if port in venue_on_port:
            raise PortMapError(f"{path}: port {port} is given to both '{venue_on_port[port]}' and '{venue}'")
        ports[str(venue)] = port
        venue_on_port[port] = str(venue)
    return ports


def allocate_port(db: Session, venue: str) -> int:
    """The venue's mapped port, or else the lowest free unreserved port in the UI range.

    If another instance holds the mapped port, a free one is used for now (with a warning)
    rather than leaving the venue without a backup; it moves to its mapped port at a later
    start once that's free (see instance_lifecycle.apply_port_map).
    """
    port_map = load_port_map()
    used_ports = {row.ui_port for row in db.query(Instance.ui_port).all()}
    if venue in port_map:
        if port_map[venue] not in used_ports:
            return port_map[venue]
        logger.warning(
            "Port %s is mapped to %s but already in use - using a free port", port_map[venue], venue
        )

    unavailable = used_ports | set(port_map.values())
    for port in range(settings.ui_port_range_start, settings.ui_port_range_end + 1):
        if port not in unavailable:
            return port

    raise NoPortsAvailableError(
        f"No free ports in range {settings.ui_port_range_start}-{settings.ui_port_range_end} - "
        "delete an unused instance or widen the range"
    )
