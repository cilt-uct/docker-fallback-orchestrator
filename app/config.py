import hashlib
import re
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


def safe_name(name: str) -> str:
    """A form of a venue/agent name that's safe as a Docker compose project, container name
    and folder name. Names that already are (every dashboard-created venue) come back
    unchanged; anything else - capitals, spaces, slashes, YAML-special characters - is
    reduced to [a-z0-9_-] with a short hash of the original appended, so two different
    names can never map to the same result.
    """
    if _SAFE_NAME.match(name):
        return name
    slug = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-_")[:40] or "venue"
    return f"{slug}-{hashlib.sha256(name.encode()).hexdigest()[:8]}"


# The value .env.example ships with for every secret.
PLACEHOLDER_SECRET = "CHANGE_ME"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Opencast, as reached by the controller process itself.
    opencast_host: str = "octestallinone.virtuos.uos.de"
    opencast_protocol: str = "https"
    opencast_username: str = "opencast_system_account"
    opencast_password: str = PLACEHOLDER_SECRET

    # Opencast, as reached from inside a managed PyCA container.
    opencast_container_host: str | None = None
    opencast_container_protocol: str | None = None

    # MySQL
    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_user: str = "pyca_orchestrator"
    mysql_password: str = PLACEHOLDER_SECRET
    mysql_database: str = "pyca_orchestrator"

    # PyCA
    pyca_image: str = "quay.io/opencast/pyca"
    instances_dir: Path = Path("./instances")
    # Recordings live here, outside the instances folder (one sub-folder per venue), so
    # they're plain files on the server rather than part of a backup's Docker state.
    recordings_dir: Path = Path("/data/pyca-recordings")
    # The user PyCA runs as in its image; a venue's recordings folder is handed to it.
    pyca_uid: int = 800
    # Where the controller reaches the backups' PyCA web UIs (published on the Docker host).
    # "localhost" when the controller runs on the host; from inside a container, the host's
    # address - e.g. host.docker.internal (see docker-compose.yml).
    pyca_ui_host: str = "localhost"
    # Login for every backup's PyCA web page - the same for all venues, so operators
    # don't have to look each one up.
    pyca_ui_username: str = "admin"
    pyca_ui_password: str = "opencast"
    # Host path of a CA bundle the backups trust for Opencast's HTTPS certificate, e.g. the
    # server's /etc/ssl/certs/ca-certificates.crt. The PyCA image's own bundle is old, so an
    # Opencast the controller trusts can still fail in PyCA with "self signed certificate".
    pyca_ca_bundle: Path | None = None

    # Fixed PyCA page ports per venue (see config/pyca-ports.example.yaml). Relative paths
    # are from the working directory - /app in the controller's container.
    pyca_ports_file: Path | None = Path("config/pyca-ports.yaml")
    ui_port_range_start: int = 9000
    ui_port_range_end: int = 9100

    poll_interval_seconds: int = 60
    schedule_lookahead_days: int = 1
    agent_offline_seconds: int = 300
    # A capture agent counts as recovered only after this long continuously healthy;
    # being down at any point within this window before a lecture also makes that
    # lecture need a backup.
    agent_stable_hours: float = 24
    # Keep a backup this long after the end of the last lecture that needed it.
    backup_retention_days: float = 4

    # Delete backup recordings this many days after they started, unless overridden per
    # recording from the UI. Applies whether or not the recording was ingested.
    recording_retention_days: int = 7
    recording_cleanup_interval_minutes: int = 60

    # Upper bound on one `docker compose` call (up can include an image pull).
    compose_timeout_seconds: int = 300

    # Local dev stacks may genuinely use the placeholder as their password; this downgrades
    # the startup check from fatal to a warning. Never set it in production.
    allow_placeholder_secrets: bool = False

    def instance_dir(self, venue: str) -> Path:
        """A venue's folder (compose file, pyca.conf, recordings), as an absolute path.

        Always derived from INSTANCES_DIR rather than stored, so the same database works
        whether the controller runs on the host or in a container that mounts the folder
        at the same path - `docker compose` passes these paths to the host's Docker daemon.
        """
        return self.instances_dir.resolve() / safe_name(venue)

    def venue_recordings_dir(self, venue: str) -> Path:
        """Where a venue's recordings are written, as an absolute path - one folder per
        recording inside, named by PyCA (recording-<start epoch>-<mediapackage id>)."""
        return self.recordings_dir.resolve() / safe_name(venue)

    def placeholder_secrets(self) -> list[str]:
        """Env var names of secrets still set to the placeholder, i.e. never configured."""
        secrets = {"OPENCAST_PASSWORD": self.opencast_password, "MYSQL_PASSWORD": self.mysql_password}
        return [name for name, value in secrets.items() if value == PLACEHOLDER_SECRET]

    @property
    def opencast_url(self) -> str:
        return f"{self.opencast_protocol}://{self.opencast_host}"

    @property
    def opencast_container_url(self) -> str:
        host = self.opencast_container_host or self.opencast_host
        protocol = self.opencast_container_protocol or self.opencast_protocol
        return f"{protocol}://{host}"

    @property
    def sqlalchemy_database_uri(self) -> str:
        return (
            f"mysql+pymysql://{self.mysql_user}:{self.mysql_password}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}"
        )


settings = Settings()
