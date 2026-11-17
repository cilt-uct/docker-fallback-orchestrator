import asyncio

import pytest

from app import audit, compose_manager, decision_engine, main
from app.config import settings
from app.models import AuditEvent, Instance
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


# ---- camera URL from Opencast ---------------------------------------------------------


def test_create_without_a_camera_takes_the_one_registered_in_opencast(client, opencast_cameras):
    opencast_cameras["lt1"] = "rtsp://lt1-cam/s"

    resp = client.post("/instances", json={"agent_id": "lt1"})

    assert resp.json()["rtsp_source"] == "rtsp://lt1-cam/s"


def test_a_camera_given_on_create_is_kept(client, opencast_cameras):
    opencast_cameras["lt1"] = "rtsp://lt1-cam/s"

    resp = client.post("/instances", json={"agent_id": "lt1", "rtsp_source": "rtsp://other/s"})

    assert resp.json()["rtsp_source"] == "rtsp://other/s"


def test_create_works_when_opencast_has_no_camera_for_the_agent(client):
    resp = client.post("/instances", json={"agent_id": "lt1"})

    assert resp.status_code == 201
    assert resp.json()["rtsp_source"] is None


def test_start_without_a_camera_looks_it_up_first(client, db, make_instance, opencast_cameras, monkeypatch):
    instance = make_instance(venue="lt1")  # no rtsp_source
    opencast_cameras["lt1"] = "rtsp://lt1-cam/s"
    started = []
    monkeypatch.setattr(instances_router, "provision_and_start", lambda i: started.append(i.rtsp_source))

    assert client.post(f"/instances/{instance.id}/start").status_code == 200

    assert started == ["rtsp://lt1-cam/s"]
    assert db.query(AuditEvent).filter_by(action=audit.INSTANCE_RTSP_SOURCE_CHANGED).count() == 1


def test_every_pass_keeps_stopped_instances_cameras_up_to_date(
    engine_db, make_instance, fake_opencast, agent_state
):
    instance_id = make_instance(venue="lt1").id  # stopped, no lecture coming, no camera yet
    oc = fake_opencast([], {"lt1": agent_state("lt1", rtsp_source="rtsp://lt1-cam/s")})

    asyncio.run(decision_engine.run_once(oc))

    assert engine_db.get(Instance, instance_id).rtsp_source == "rtsp://lt1-cam/s"


def test_start_with_no_camera_anywhere_stays_null_and_is_refused(client, db, make_instance):
    instance_id = make_instance(venue="lt1").id  # no rtsp_source, and none in Opencast

    resp = client.post(f"/instances/{instance_id}/start")

    assert resp.status_code == 409
    assert "No RTSP source known" in resp.json()["detail"]
    assert db.get(Instance, instance_id).rtsp_source is None


@pytest.mark.parametrize("existing", [None, "rtsp://typed-in/s"])
def test_a_pass_never_clears_a_camera_when_opencast_has_none(
    engine_db, make_instance, fake_opencast, agent_state, existing
):
    instance_id = make_instance(venue="lt1", rtsp_source=existing).id
    oc = fake_opencast([], {"lt1": agent_state("lt1", rtsp_source=None)})

    asyncio.run(decision_engine.run_once(oc))

    assert engine_db.get(Instance, instance_id).rtsp_source == existing
