from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Opencast, as reached by the controller process itself.
    opencast_host: str = "octestallinone.virtuos.uos.de"
    opencast_protocol: str = "https"
    opencast_username: str = "opencast_system_account"
    opencast_password: str = "CHANGE_ME"

    # Opencast, as reached from inside a managed PyCA container.
    opencast_container_host: str | None = None
    opencast_container_protocol: str | None = None

    # MySQL
    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_user: str = "pyca_orchestrator"
    mysql_password: str = "CHANGE_ME"
    mysql_database: str = "pyca_orchestrator"

    # PyCA
    pyca_image: str = "quay.io/opencast/pyca"
    instances_dir: Path = Path("./instances")

    ui_port_range_start: int = 9000
    ui_port_range_end: int = 9100

    poll_interval_seconds: int = 60
    schedule_lookahead_days: int = 1
    recording_lead_minutes: int = 15
    agent_offline_seconds: int = 300

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
