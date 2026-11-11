from pathlib import Path
from string import Template

from app.config import settings
from app.models import Instance

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "pyca.conf.template"
# Where PYCA_CA_BUNDLE is mounted inside each PyCA container.
CA_BUNDLE_MOUNT = "/etc/pyca/ca-bundle.pem"


def ui_credentials() -> tuple[str, str]:
    return settings.pyca_ui_username, settings.pyca_ui_password


def render_pyca_conf(instance: Instance, ui_username: str, ui_password: str) -> str:
    template = Template(TEMPLATE_PATH.read_text())
    return template.substitute(
        venue=instance.venue,
        agent_id=instance.agent_id,
        rtsp_source=instance.rtsp_source or "",
        opencast_url=settings.opencast_container_url,
        opencast_username=settings.opencast_username,
        opencast_password=settings.opencast_password,
        ui_username=ui_username,
        ui_password=ui_password,
        ui_port=instance.ui_port,
        certificate=CA_BUNDLE_MOUNT if settings.pyca_ca_bundle else "",
    )


def write_pyca_conf(instance: Instance, ui_username: str, ui_password: str) -> Path:
    config_dir = settings.instance_dir(instance.venue)
    config_dir.mkdir(parents=True, exist_ok=True)
    conf_path = config_dir / "pyca.conf"
    conf_path.write_text(render_pyca_conf(instance, ui_username, ui_password))
    return conf_path
