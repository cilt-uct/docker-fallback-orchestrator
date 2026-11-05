import asyncio
from datetime import timedelta

from app import audit, decision_engine
from app.models import AuditActorType, AuditEvent, AuditOutcome, Instance, InstanceStatus
from app.routers import instances as instances_router


def _events(db):
    return db.query(AuditEvent).order_by(AuditEvent.id).all()


def test_redact_url_strips_credentials():
    assert audit.redact_url("rtsp://admin:s3cret@cam.example:554/stream") == "rtsp://cam.example:554/stream"
    assert audit.redact_url("rtsp://cam.example/stream") == "rtsp://cam.example/stream"
    assert audit.redact_url(None) is None


def test_record_snapshots_instance_and_system_actor(db, make_instance):
    instance = make_instance()
    audit.record(db, actor=audit.SYSTEM, action=audit.INSTANCE_START, instance=instance)
    db.commit()

    (event,) = _events(db)
    assert event.actor_type == AuditActorType.system
    assert event.actor == "system"
    assert event.source_ip is None
    assert (event.instance_id, event.venue, event.agent_id) == (instance.id, "room-1", "room-1")
    assert event.outcome == AuditOutcome.success
    assert event.occurred_at is not None


def test_create_is_audited_with_null_operator_and_redacted_source(client, db):
    resp = client.post(
        "/instances",
        json={"venue": "room-1", "agent_id": "agent-1", "rtsp_source": "rtsp://u:p@cam/stream"},
    )
    assert resp.status_code == 201

    (event,) = _events(db)
    assert event.action == audit.INSTANCE_CREATE
    assert event.actor_type == AuditActorType.operator
    assert event.actor is None
    assert event.source_ip == "testclient"
    assert event.instance_id == resp.json()["id"]
    assert event.detail["rtsp_source"] == "rtsp://cam/stream"


def test_duplicate_create_is_audited_as_failure(client, db, make_instance):
    make_instance()
    resp = client.post("/instances", json={"venue": "room-1", "agent_id": "agent-1"})
    assert resp.status_code == 409

    (event,) = _events(db)
    assert event.outcome == AuditOutcome.failure
    assert event.venue == "room-1"
    assert event.instance_id is None


def test_start_failure_is_audited_with_error(client, db, make_instance):
    instance = make_instance()  # no rtsp_source, so provision_and_start refuses
    resp = client.post(f"/instances/{instance.id}/start")
    assert resp.status_code == 409

    (event,) = _events(db)
    assert event.action == audit.INSTANCE_START
    assert event.outcome == AuditOutcome.failure
    assert "No RTSP source" in event.detail["error"]


def test_stop_and_restart_are_audited(client, db, monkeypatch, make_instance):
    monkeypatch.setattr(instances_router.compose_manager, "down", lambda instance: None)
    monkeypatch.setattr(instances_router.compose_manager, "restart", lambda instance: None)
    instance = make_instance(status=InstanceStatus.running)

    assert client.post(f"/instances/{instance.id}/restart").status_code == 200
    assert client.post(f"/instances/{instance.id}/stop").status_code == 200

    assert [(e.action, e.outcome) for e in _events(db)] == [
        (audit.INSTANCE_RESTART, AuditOutcome.success),
        (audit.INSTANCE_STOP, AuditOutcome.success),
    ]


def test_delete_history_outlives_the_instance(client, db, make_instance):
    instance = make_instance()
    instance_id = instance.id
    assert client.delete(f"/instances/{instance_id}").status_code == 204

    assert db.get(Instance, instance_id) is None
    (event,) = _events(db)
    assert event.action == audit.INSTANCE_DELETE
    assert (event.instance_id, event.venue) == (instance_id, "room-1")


def test_delete_of_running_instance_is_audited_as_failure(client, db, make_instance):
    instance = make_instance(status=InstanceStatus.running)
    assert client.delete(f"/instances/{instance.id}").status_code == 409

    (event,) = _events(db)
    assert event.action == audit.INSTANCE_DELETE
    assert event.outcome == AuditOutcome.failure


def test_reads_are_not_audited(client, db, make_instance):
    make_instance()
    assert client.get("/instances").status_code == 200
    assert _events(db) == []


def test_audit_endpoint_filters_and_pages(client, db, make_instance):
    a = make_instance(venue="room-a")
    b = make_instance(venue="room-b")
    for instance in (a, b, a):
        audit.record(db, actor=audit.SYSTEM, action=audit.INSTANCE_START, instance=instance)
    audit.record(
        db,
        actor=audit.Actor(type=AuditActorType.operator, source_ip="10.0.0.1"),
        action=audit.INSTANCE_STOP,
        outcome=AuditOutcome.failure,
        instance=a,
    )
    db.commit()

    newest_first = client.get("/audit").json()
    assert [e["id"] for e in newest_first] == sorted((e["id"] for e in newest_first), reverse=True)
    assert len(newest_first) == 4

    assert len(client.get("/audit", params={"venue": "room-a"}).json()) == 3
    assert len(client.get("/audit", params={"instance_id": b.id}).json()) == 1
    assert len(client.get("/audit", params={"actor_type": "operator"}).json()) == 1
    assert len(client.get("/audit", params={"outcome": "failure"}).json()) == 1
    assert len(client.get("/audit", params={"action": audit.INSTANCE_START}).json()) == 3

    page = client.get("/audit", params={"limit": 2}).json()
    rest = client.get("/audit", params={"limit": 2, "before_id": page[-1]["id"]}).json()
    assert [e["id"] for e in page + rest] == [e["id"] for e in newest_first]


def test_engine_audits_a_repeating_start_failure_once(
    engine_db, monkeypatch, fake_opencast, scheduled_event, offline_agent
):

    def fail(instance):
        raise RuntimeError("compose up failed")

    monkeypatch.setattr(decision_engine, "provision_and_start", fail)
    oc = fake_opencast([scheduled_event()], {"room-1": offline_agent()})

    asyncio.run(decision_engine.run_once(oc))
    asyncio.run(decision_engine.run_once(oc))

    events = _events(engine_db)
    assert [(e.action, e.outcome, e.actor) for e in events] == [
        (audit.INSTANCE_CREATE, AuditOutcome.success, "system"),
        (audit.INSTANCE_START, AuditOutcome.failure, "system"),
    ]
    assert events[1].detail == {
        "reason": "agent_offline",
        "mediapackage_id": "mp-room-1",
        "error": "compose up failed",
    }

    monkeypatch.setattr(decision_engine, "provision_and_start", lambda instance: None)
    asyncio.run(decision_engine.run_once(oc))
    assert _events(engine_db)[-1].outcome == AuditOutcome.success


def test_engine_audits_deferred_teardown_once_then_the_stop(
    engine_db, monkeypatch, make_instance, fake_opencast, agent_state, seed_health
):
    instance = make_instance(status=InstanceStatus.running)
    # The agent has been healthy long enough, so only the PyCA's own work holds the backup up.
    seed_health("room-1", healthy_for=timedelta(hours=25))
    # It was started for two events, and an older lifetime (closed by a stop) for another.
    for action, mediapackage_id in (
        (audit.INSTANCE_START, "mp-old"),
        (audit.INSTANCE_STOP, None),
        (audit.INSTANCE_START, "mp-1"),
        (audit.INSTANCE_START, "mp-2"),
    ):
        audit.record(
            engine_db,
            actor=audit.SYSTEM,
            action=action,
            instance=instance,
            detail={"mediapackage_id": mediapackage_id} if mediapackage_id else None,
        )
    engine_db.commit()
    seeded = len(_events(engine_db))
    activity = decision_engine.BackupReason.pending_ingest

    async def pyca_activity(instance):
        return activity

    monkeypatch.setattr(decision_engine, "pyca_activity", pyca_activity)
    monkeypatch.setattr(decision_engine.compose_manager, "down", lambda instance: None)
    oc = fake_opencast([], {"room-1": agent_state()})

    asyncio.run(decision_engine.run_once(oc))
    asyncio.run(decision_engine.run_once(oc))
    deferred = _events(engine_db)[seeded:]
    assert [e.action for e in deferred] == [audit.INSTANCE_TEARDOWN_DEFERRED]
    assert deferred[0].detail == {"reason": "pending_ingest", "mediapackage_ids": ["mp-1", "mp-2"]}

    activity = None
    asyncio.run(decision_engine.run_once(oc))
    stop = _events(engine_db)[-1]
    assert (stop.action, stop.actor) == (audit.INSTANCE_STOP, "system")
    assert stop.detail["reason"] == "no_longer_needed"
    assert stop.detail["mediapackage_ids"] == ["mp-1", "mp-2"]
    assert stop.detail["agent_healthy_since"]
    assert stop.detail["last_needed_lecture_end"] is None


def test_dashboard_links_to_the_audit_log_page(client):
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert 'href="/activity"' in dashboard.text

    page = client.get("/activity")
    assert page.status_code == 200
    assert "/static/audit.js" in page.text


def test_manual_stop_records_the_mediapackages_since_the_last_start(client, db, make_instance, monkeypatch):
    monkeypatch.setattr(instances_router.compose_manager, "down", lambda instance: None)
    instance = make_instance(status=InstanceStatus.running)
    audit.record(
        db,
        actor=audit.SYSTEM,
        action=audit.INSTANCE_START,
        instance=instance,
        detail={"mediapackage_id": "mp-9"},
    )
    db.commit()

    assert client.post(f"/instances/{instance.id}/stop").status_code == 200
    assert "mediapackage_id" not in client.get("/instances").json()[0]

    stop = _events(db)[-1]
    assert (stop.action, stop.detail) == (audit.INSTANCE_STOP, {"mediapackage_ids": ["mp-9"]})
    # A fresh lifetime starts empty once it has been stopped.
    assert audit.mediapackages_since_last_stop(db, instance.id) == []
