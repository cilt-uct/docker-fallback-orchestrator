"""Generates and drives the per-instance docker-compose stack.

Docker Compose stacks are not something the Docker SDK for Python
(the `docker` package) can apply directly - it operates on containers,
images, networks and volumes, not compose files. So stack lifecycle
(up/down) shells out to the `docker compose` CLI against the generated
file, while read-only monitoring (container status, health) goes through
the SDK by filtering on the `com.docker.compose.project` label.
"""

import subprocess
from pathlib import Path

import docker
from jinja2 import Environment, FileSystemLoader

from app.config import settings
from app.models import Instance

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_jinja_env = Environment(loader=FileSystemLoader(TEMPLATES_DIR))

_docker_client = docker.from_env()


class ComposeError(RuntimeError):
    pass


def render_compose_file(instance: Instance) -> Path:
    template = _jinja_env.get_template("docker-compose.yaml.j2")
    content = template.render(pyca_image=settings.pyca_image, ui_port=instance.ui_port)

    config_dir = Path(instance.config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    compose_path = config_dir / "docker-compose.yaml"
    compose_path.write_text(content)
    return compose_path


def _run_compose(instance: Instance, *args: str) -> None:
    compose_path = Path(instance.config_dir) / "docker-compose.yaml"
    result = subprocess.run(
        ["docker", "compose", "-p", instance.compose_project, "-f", str(compose_path), *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ComposeError(
            f"docker compose {' '.join(args)} failed for {instance.venue}: {result.stderr.strip()}"
        )


def up(instance: Instance) -> None:
    _run_compose(instance, "up", "-d")


def down(instance: Instance) -> None:
    _run_compose(instance, "down")


def restart(instance: Instance) -> None:
    _run_compose(instance, "restart")


def container_statuses(instance: Instance) -> list[dict]:
    containers = _docker_client.containers.list(
        all=True,
        filters={"label": f"com.docker.compose.project={instance.compose_project}"},
    )
    return [
        {
            "name": c.name,
            "status": c.status,
            "health": (c.attrs.get("State", {}).get("Health") or {}).get("Status"),
        }
        for c in containers
    ]


def is_running(instance: Instance) -> bool:
    statuses = container_statuses(instance)
    return bool(statuses) and all(s["status"] == "running" for s in statuses)
