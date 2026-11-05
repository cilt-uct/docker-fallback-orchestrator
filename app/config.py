from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

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

    # Upper bound on one `docker compose` call (up can include an image pull).
    compose_timeout_seconds: int = 300

    # Local dev stacks may genuinely use the placeholder as their password; this downgrades
    # the startup check from fatal to a warning. Never set it in production.
    allow_placeholder_secrets: bool = False

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
