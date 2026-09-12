from sqlalchemy.orm import Session

from app.config import settings
from app.models import Instance


class NoPortsAvailableError(RuntimeError):
    pass


def allocate_port(db: Session) -> int:
    """Return the lowest free pyca-ui port in the configured range."""
    used_ports = {row.ui_port for row in db.query(Instance.ui_port).all()}

    for port in range(settings.ui_port_range_start, settings.ui_port_range_end + 1):
        if port not in used_ports:
            return port

    raise NoPortsAvailableError(
        f"No free ports in range {settings.ui_port_range_start}-{settings.ui_port_range_end}"
    )
