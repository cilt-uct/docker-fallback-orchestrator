import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import audit, decision_engine
from app.database import get_db
from app.models import AuditActorType, AuditEvent, AuditOutcome, Instance, InstanceStatus
from app.opencast_client import AgentState, ScheduledEvent
from app.routers import audit as audit_router
from app.routers import instances as instances_router
from app.routers import web as web_router


def _instance(db, venue="room-1", status=InstanceStatus.stopped, **kwargs):
    instance = Instance(
        venue=venue,
        agent_id=venue,
        ui_port=9000 + db.query(Instance).count(),
        compose_project=f"pyca-{venue}",
        config_dir=f"/tmp/{venue}",
        status=status,
        **kwargs,
    )
    db.add(instance)
    db.commit()
    return instance


def _events(db):
    return db.query(AuditEvent).order_by(AuditEvent.id).all()


@pytest.fixture
def client(db):
    app = FastAPI()
    app.include_router(instances_router.router)
    app.include_router(audit_router.router)
    app.include_router(web_router.router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def test_redact_url_strips_credentials():
    assert audit.redact_url("rtsp://admin:s3cret@cam.example:554/stream") == "rtsp://cam.example:554/stream"
    assert audit.redact_url("rtsp://cam.example/stream") == "rtsp://cam.example/stream"
    assert audit.redact_url(None) is None


def test_record_snapshots_instance_and_system_actor(db):
    instance = _instance(db)
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


def test_duplicate_create_is_audited_as_failure(client, db):
    _instance(db)
    resp = client.post("/instances", json={"venue": "room-1", "agent_id": "agent-1"})
    assert resp.status_code == 409

    (event,) = _events(db)
    assert event.outcome == AuditOutcome.failure
    assert event.venue == "room-1"
    assert event.instance_id is None


def test_start_failure_is_audited_with_error(client, db):
    instance = _instance(db)  # no rtsp_source, so provision_and_start refuses
    resp = client.post(f"/instances/{instance.id}/start")
    assert resp.status_code == 409

    (event,) = _events(db)
    assert event.action == audit.INSTANCE_START
    assert event.outcome == AuditOutcome.failure
    assert "No RTSP source" in event.detail["error"]


def test_stop_and_restart_are_audited(client, db, monkeypatch):
    monkeypatch.setattr(instances_router.compose_manager, "down", lambda instance: None)
    monkeypatch.setattr(instances_router.compose_manager, "restart", lambda instance: None)
    instance = _instance(db, status=InstanceStatus.running)

    assert client.post(f"/instances/{instance.id}/restart").status_code == 200
    assert client.post(f"/instances/{instance.id}/stop").status_code == 200

    assert [(e.action, e.outcome) for e in _events(db)] == [
        (audit.INSTANCE_RESTART, AuditOutcome.success),
        (audit.INSTANCE_STOP, AuditOutcome.success),
    ]


def test_delete_history_outlives_the_instance(client, db):
    instance = _instance(db)
    instance_id = instance.id
    assert client.delete(f"/instances/{instance_id}").status_code == 204

    assert db.get(Instance, instance_id) is None
    (event,) = _events(db)
    assert event.action == audit.INSTANCE_DELETE
    assert (event.instance_id, event.venue) == (instance_id, "room-1")


def test_delete_of_running_instance_is_audited_as_failure(client, db):
    instance = _instance(db, status=InstanceStatus.running)
    assert client.delete(f"/instances/{instance.id}").status_code == 409

    (event,) = _events(db)
    assert event.action == audit.INSTANCE_DELETE
    assert event.outcome == AuditOutcome.failure


def test_reads_are_not_audited(client, db):
    _instance(db)
    assert client.get("/instances").status_code == 200
    assert _events(db) == []


def test_audit_endpoint_filters_and_pages(client, db):
    a = _instance(db, venue="room-a")
    b = _instance(db, venue="room-b")
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


def _scheduled_event(minutes_to_start):
    start = datetime.now(timezone.utc) + timedelta(minutes=minutes_to_start)
    return ScheduledEvent(
        mediapackage_id="mp-1", title="Lecture", agent_id="room-1", start=start, end=start + timedelta(hours=1)
    )


def test_backup_reason(monkeypatch):
    monkeypatch.setattr(decision_engine.settings, "recording_lead_minutes", 15)
    soon, later = _scheduled_event(5), _scheduled_event(120)

    assert decision_engine._backup_reason(later, agent_healthy=True, agent_offline=True) == "agent_offline"
    assert decision_engine._backup_reason(soon, agent_healthy=False, agent_offline=False) == "unhealthy_near_start"
    assert decision_engine._backup_reason(later, agent_healthy=False, agent_offline=False) is None
    assert decision_engine._backup_reason(soon, agent_healthy=True, agent_offline=False) is None


class _FakeOpencast:
    def __init__(self, events, agent_state=None):
        self.events = events
        self.agent_state = agent_state

    async def get_scheduled_events(self):
        return self.events

    async def get_agent_state(self, agent_id):
        return self.agent_state


@pytest.fixture
def engine_db(db, monkeypatch):
    monkeypatch.setattr(decision_engine, "SessionLocal", lambda: db)
    monkeypatch.setattr(decision_engine, "_teardown_deferred_ids", set())
    return db


def test_engine_audits_a_repeating_start_failure_once(engine_db, monkeypatch):
    offline = AgentState(
        agent_id="room-1", state="unknown", seconds_since_update=1e9, rtsp_source="rtsp://cam/s"
    )

    def fail(instance):
        raise RuntimeError("compose up failed")

    monkeypatch.setattr(decision_engine, "provision_and_start", fail)
    oc = _FakeOpencast([_scheduled_event(5)], offline)

    asyncio.run(decision_engine.run_once(oc))
    asyncio.run(decision_engine.run_once(oc))

    events = _events(engine_db)
    assert [(e.action, e.outcome, e.actor) for e in events] == [
        (audit.INSTANCE_CREATE, AuditOutcome.success, "system"),
        (audit.INSTANCE_START, AuditOutcome.failure, "system"),
    ]
    assert events[1].detail == {
        "reason": "agent_offline",
        "mediapackage_id": "mp-1",
        "error": "compose up failed",
    }

    monkeypatch.setattr(decision_engine, "provision_and_start", lambda instance: None)
    asyncio.run(decision_engine.run_once(oc))
    assert _events(engine_db)[-1].outcome == AuditOutcome.success


def test_engine_audits_deferred_teardown_once_then_the_stop(engine_db, monkeypatch):
    _instance(engine_db, status=InstanceStatus.running, mediapackage_id="mp-1")
    pending = True

    async def has_pending_ingest(instance):
        return pending

    monkeypatch.setattr(decision_engine, "has_pending_ingest", has_pending_ingest)
    monkeypatch.setattr(decision_engine.compose_manager, "down", lambda instance: None)
    oc = _FakeOpencast([])

    asyncio.run(decision_engine.run_once(oc))
    asyncio.run(decision_engine.run_once(oc))
    assert [e.action for e in _events(engine_db)] == [audit.INSTANCE_TEARDOWN_DEFERRED]

    pending = False
    asyncio.run(decision_engine.run_once(oc))
    stop = _events(engine_db)[-1]
    assert (stop.action, stop.actor) == (audit.INSTANCE_STOP, "system")
    assert stop.detail == {"reason": "no_longer_needed", "mediapackage_id": "mp-1"}


def test_dashboard_links_to_the_audit_log_page(client):
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert 'href="/activity"' in dashboard.text

    page = client.get("/activity")
    assert page.status_code == 200
    assert "/static/audit.js" in page.text
