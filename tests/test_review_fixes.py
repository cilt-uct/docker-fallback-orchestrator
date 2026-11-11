"""Fixes from the code review of 2026-11-05: orphaned containers, status drift, engine/API
races, blocking Docker calls, unsafe venue names, and the manual-start credentials window."""

import asyncio
import threading
from datetime import timedelta

import pytest

from app import audit, compose_manager, decision_engine, instance_lifecycle
from app.config import safe_name, settings
from app.instance_lifecycle import InstanceBusyError, instance_lock
from app.models import AuditEvent, AuditOutcome, Instance, InstanceStatus
from app.routers import instances as instances_router

# ---- 5. unsafe venue names ----------------------------------------------------------------


@pytest.mark.parametrize("name", ["lt1", "pyca-container", "lt-1_b", "room9"])
def test_safe_names_are_kept_as_they_are(name):
    assert safe_name(name) == name


@pytest.mark.parametrize("name", ["LT 1", "../etc", "Room: A#1", "", "Ünïcode", "a/b", "x" * 80])
def test_unsafe_names_become_valid_docker_and_folder_names(name):
    result = safe_name(name)
    assert result.replace("-", "").replace("_", "").isalnum() and result.islower() or result.isdigit()
    assert "/" not in result and ".." not in result and len(result) <= 63
    assert safe_name(result) == result  # stable once safe


def test_different_unsafe_names_never_collide():
    assert safe_name("LT 1") != safe_name("lt-1") != safe_name("lt 1")


def test_engine_created_instance_from_an_unsafe_agent_id_gets_valid_docker_names(db, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "instances_dir", tmp_path / "i")
    monkeypatch.setattr(settings, "recordings_dir", tmp_path / "r")
    instance = instance_lifecycle.build_instance(db, "Room A: #1", "Room A: #1", "rtsp://cam/s")
    db.add(instance)
    db.commit()

    compose = compose_manager.render_compose_file(instance).read_text()

    name = safe_name("Room A: #1")
    assert instance.compose_project == f"pyca-{name}"
    assert f"container_name: pyca-{name}-capture" in compose
    assert settings.instance_dir(instance.venue) == (tmp_path / "i").resolve() / name
    assert "Room A" not in compose.split("services:")[1]  # never injected raw into the YAML


# ---- 1. deleting must not orphan containers ------------------------------------------------


def test_delete_stops_leftover_containers_first(client, db, make_instance, monkeypatch):
    stopped = []
    monkeypatch.setattr(instances_router.compose_manager, "down", lambda i: stopped.append(i.venue))
    instance_id = make_instance(status=InstanceStatus.error).id  # e.g. after a failed stop

    assert client.delete(f"/instances/{instance_id}").status_code == 204
    assert stopped == ["room-1"]
    assert db.get(Instance, instance_id) is None


def test_delete_is_refused_if_the_containers_cannot_be_stopped(client, db, make_instance, monkeypatch):
    def fail(instance):
        raise compose_manager.ComposeError("docker compose down failed for room-1: boom")

    monkeypatch.setattr(instances_router.compose_manager, "down", fail)
    instance_id = make_instance(status=InstanceStatus.error).id

    assert client.delete(f"/instances/{instance_id}").status_code == 502
    assert db.get(Instance, instance_id) is not None


def test_down_is_a_no_op_for_a_backup_that_was_never_started(monkeypatch, tmp_path, make_instance):
    monkeypatch.setattr(settings, "instances_dir", tmp_path)
    ran = []
    monkeypatch.setattr(compose_manager, "_run_compose", lambda *a: ran.append(a))

    compose_manager.down(make_instance())  # no compose file written yet
    assert ran == []


def test_engine_tears_down_instances_left_in_error(
    engine_db, make_instance, fake_opencast, agent_state, seed_health, monkeypatch
):
    make_instance(status=InstanceStatus.error)  # failed stop: containers may still be up
    seed_health(healthy_for=timedelta(days=5))
    stopped = []

    async def idle(instance):
        return None

    monkeypatch.setattr(decision_engine, "pyca_activity", idle)
    monkeypatch.setattr(decision_engine.compose_manager, "down", lambda i: stopped.append(i.venue))

    asyncio.run(decision_engine.run_once(fake_opencast([], {"room-1": agent_state()})))

    assert stopped == ["room-1"]
    assert engine_db.query(Instance).one().status == InstanceStatus.stopped


# ---- 2. status drift: marked running, containers gone ------------------------------------


def test_running_backup_whose_containers_vanished_is_marked_error(
    engine_db, make_instance, fake_opencast, agent_state, seed_health, monkeypatch
):
    instance_id = make_instance(status=InstanceStatus.running).id
    seed_health(healthy_for=timedelta(hours=1))  # not due for teardown
    monkeypatch.setattr(decision_engine.compose_manager, "has_running_containers", lambda i: False)

    asyncio.run(decision_engine.run_once(fake_opencast([], {"room-1": agent_state()})))

    instance = engine_db.get(Instance, instance_id)
    assert instance.status == InstanceStatus.error
    assert "containers aren't running" in instance.last_error
    lost = engine_db.query(AuditEvent).filter_by(action=audit.INSTANCE_CONTAINERS_LOST).one()
    assert lost.outcome == AuditOutcome.failure


def test_unreachable_pyca_with_no_containers_no_longer_blocks_teardown(
    engine_db, make_instance, fake_opencast, agent_state, seed_health, monkeypatch
):
    make_instance(status=InstanceStatus.error)
    seed_health(healthy_for=timedelta(days=5))
    stopped = []

    async def unreachable(instance):
        return decision_engine.BackupReason.pyca_unreachable

    monkeypatch.setattr(decision_engine, "pyca_activity", unreachable)
    monkeypatch.setattr(decision_engine.compose_manager, "has_running_containers", lambda i: False)
    monkeypatch.setattr(decision_engine.compose_manager, "down", lambda i: stopped.append(i.venue))

    asyncio.run(decision_engine.run_once(fake_opencast([], {"room-1": agent_state()})))

    assert stopped == ["room-1"]


def test_unreachable_pyca_with_containers_up_still_blocks_teardown(
    engine_db, make_instance, fake_opencast, agent_state, seed_health, monkeypatch
):
    make_instance(status=InstanceStatus.running)
    seed_health(healthy_for=timedelta(days=5))

    async def unreachable(instance):
        return decision_engine.BackupReason.pyca_unreachable

    monkeypatch.setattr(decision_engine, "pyca_activity", unreachable)
    monkeypatch.setattr(decision_engine.compose_manager, "down", lambda i: pytest.fail("must not stop"))

    asyncio.run(decision_engine.run_once(fake_opencast([], {"room-1": agent_state()})))

    assert engine_db.query(Instance).one().status == InstanceStatus.running


# ---- 3. engine and API never act on the same backup at once --------------------------------


def test_instance_lock_is_exclusive():
    with instance_lock(42):
        with pytest.raises(InstanceBusyError):
            with instance_lock(42):
                pass
        with instance_lock(43):  # other instances unaffected
            pass
    with instance_lock(42):  # released afterwards
        pass


def test_api_answers_409_while_the_engine_holds_the_backup(client, make_instance):
    instance_id = make_instance(status=InstanceStatus.running).id
    with instance_lock(instance_id):
        resp = client.post(f"/instances/{instance_id}/stop")
    assert resp.status_code == 409
    assert "try again" in resp.json()["detail"]


def test_engine_skips_a_backup_the_dashboard_is_changing(
    engine_db, make_instance, fake_opencast, agent_state, seed_health, monkeypatch
):
    instance_id = make_instance(status=InstanceStatus.running).id
    seed_health(healthy_for=timedelta(days=5))  # due for teardown
    monkeypatch.setattr(decision_engine.compose_manager, "down", lambda i: pytest.fail("must not stop"))

    with instance_lock(instance_id):
        asyncio.run(decision_engine.run_once(fake_opencast([], {"room-1": agent_state()})))

    assert engine_db.get(Instance, instance_id).status == InstanceStatus.running


# ---- 4. Docker calls off the event loop, and backoff for failing venues ---------------------


def test_engine_runs_docker_calls_off_the_event_loop(
    engine_db, fake_opencast, offline_agent, scheduled_event, monkeypatch
):
    threads = []
    monkeypatch.setattr(
        decision_engine, "provision_and_start", lambda i: threads.append(threading.current_thread())
    )

    asyncio.run(decision_engine.run_once(fake_opencast([scheduled_event()], {"room-1": offline_agent()})))

    assert threads and threads[0] is not threading.main_thread()


def test_failed_starts_back_off_instead_of_retrying_every_poll(
    engine_db, fake_opencast, offline_agent, scheduled_event, monkeypatch
):
    attempts = []

    def fail(instance):
        attempts.append(1)
        raise compose_manager.ComposeError("docker compose up -d failed for room-1: boom")

    monkeypatch.setattr(decision_engine, "provision_and_start", fail)
    oc = fake_opencast([scheduled_event()], {"room-1": offline_agent()})

    for _ in range(4):
        asyncio.run(decision_engine.run_once(oc))

    assert len(attempts) == 1  # the next try waits a poll interval, then 2, 4... up to 15 minutes
    failures, retry_at = next(iter(decision_engine._start_failures.values()))
    assert failures == 1 and retry_at > decision_engine.utcnow()


def test_backoff_doubles_and_is_capped():
    now = decision_engine.utcnow()
    decision_engine._start_failures.clear()
    delays = []
    for _ in range(8):
        decision_engine._record_start_failure(7, now)
        delays.append(decision_engine._start_failures[7][1] - now)
    step = timedelta(seconds=settings.poll_interval_seconds)
    assert delays[:3] == [step, step * 2, step * 4]
    assert delays[-1] == timedelta(minutes=15)
    decision_engine._start_failures.clear()


# ---- 6. manual start saves the login before Docker -----------------------------------------


def test_manual_start_saves_ui_credentials_before_docker(client, db, make_instance, monkeypatch):
    seen = {}

    def docker_starts(instance):
        # At the moment Docker would run, the login and "starting" must already be committed:
        # nothing about the instance pending in the session.
        seen.update(
            pending=instance in db.dirty,
            status=instance.status,
            has_login=bool(instance.ui_username and instance.ui_password),
        )

    monkeypatch.setattr(instances_router, "provision_and_start", docker_starts)
    instance_id = make_instance(rtsp_source="rtsp://cam/s").id

    assert client.post(f"/instances/{instance_id}/start").status_code == 200
    assert seen == {"pending": False, "status": InstanceStatus.starting, "has_login": True}


# ---- 7. restart applies current config; 8. Manual flag on every response --------------------


def test_restart_rewrites_config_and_stops_then_starts(client, make_instance, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "instances_dir", tmp_path / "i")
    monkeypatch.setattr(settings, "recordings_dir", tmp_path / "r")
    ran = []
    monkeypatch.setattr(compose_manager, "_run_compose", lambda instance, *args: ran.append(args))
    instance = make_instance(status=InstanceStatus.running, rtsp_source="rtsp://new-cam/s")
    compose_manager.render_compose_file(instance)  # it was started before

    assert client.post(f"/instances/{instance.id}/restart").status_code == 200

    # A full stop first, so events the stop interrupts can be repaired before PyCA restarts.
    assert ran == [("down",), ("up", "-d")]
    assert "rtsp://new-cam/s" in (tmp_path / "i" / "room-1" / "pyca.conf").read_text()


def test_manual_flag_is_on_start_and_details_responses(client, make_instance, monkeypatch):
    monkeypatch.setattr(instances_router, "provision_and_start", lambda instance: None)
    monkeypatch.setattr(instances_router.compose_manager, "container_statuses", lambda instance: [])
    instance_id = make_instance(rtsp_source="rtsp://cam/s").id

    assert client.post(f"/instances/{instance_id}/start").json()["started_by_operator"] is True
    assert client.get(f"/instances/{instance_id}").json()["instance"]["started_by_operator"] is True
