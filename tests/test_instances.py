import asyncio

import pytest

from app import compose_manager, main
from app.config import settings
from app.routers import instances as instances_router


def test_details_include_the_recordings_size(client, make_instance, monkeypatch, tmp_path):
    monkeypatch.setattr(instances_router.compose_manager, "container_statuses", lambda instance: [])
    monkeypatch.setattr(settings, "recordings_dir", tmp_path)
    instance = make_instance()
    recordings = tmp_path / "room-1"
    recordings.mkdir(parents=True)
    (recordings / "lecture.mp4").write_bytes(b"x" * 1234)

    resp = client.get(f"/instances/{instance.id}")

    assert resp.status_code == 200
    assert resp.json()["recordings_bytes"] == 1234


def test_summary_reports_disk_usage_for_the_recordings_dir_even_with_no_instances(
    client, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "recordings_dir", tmp_path)

    disk = client.get("/monitoring/summary").json()["disk_usage"]

    assert disk is not None
    assert disk["total"] > 0


def test_placeholder_secrets_are_reported(monkeypatch):
    monkeypatch.setattr(settings, "opencast_password", "CHANGE_ME")
    monkeypatch.setattr(settings, "mysql_password", "s3cret")

    assert settings.placeholder_secrets() == ["OPENCAST_PASSWORD"]


def _start(app_module) -> None:
    """Run just the startup half of the app's lifespan."""

    async def enter():
        async with app_module.lifespan(app_module.app):
            pass

    asyncio.run(enter())


def test_startup_refuses_placeholder_secrets_unless_allowed(monkeypatch, caplog):
    monkeypatch.setattr(settings, "opencast_password", "CHANGE_ME")
    monkeypatch.setattr(settings, "allow_placeholder_secrets", False)
    with pytest.raises(RuntimeError, match="OPENCAST_PASSWORD"):
        _start(main)

    # Allowed: startup continues past the check (stub out the DB and engine it would start next).
    monkeypatch.setattr(settings, "allow_placeholder_secrets", True)
    monkeypatch.setattr(main.Base.metadata, "create_all", lambda bind: None)

    async def idle():
        pass

    monkeypatch.setattr(main, "run_forever", idle)
    monkeypatch.setattr(main, "run_cleanup_forever", idle)
    _start(main)
    assert "ALLOW_PLACEHOLDER_SECRETS" in caplog.text


def test_backup_containers_are_named_after_the_venue(make_instance, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "instances_dir", tmp_path)
    instance = make_instance(venue="lt1")

    compose = compose_manager.render_compose_file(instance).read_text()

    names = sorted(line.split(": ")[1] for line in compose.splitlines() if "container_name:" in line)
    assert names == [
        "pyca-lt1-agentstate",
        "pyca-lt1-capture",
        "pyca-lt1-ingest",
        "pyca-lt1-schedule",
        "pyca-lt1-ui",
    ]
