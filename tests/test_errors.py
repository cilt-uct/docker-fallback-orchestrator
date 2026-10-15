import asyncio
import subprocess
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from docker.errors import DockerException
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app import audit, compose_manager, decision_engine
from app.database import get_db
from app.errors import register_exception_handlers
from app.models import AuditEvent, AuditOutcome, Instance, InstanceStatus
from app.opencast_client import AgentState, OpencastClient, OpencastError, ScheduledEvent
from app.port_allocator import NoPortsAvailableError
from app.routers import instances as instances_router
from app.routers import monitoring as monitoring_router


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


def _app(db):
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(instances_router.router)
    app.include_router(monitoring_router.router)
    app.dependency_overrides[get_db] = lambda: db
    return app


@pytest.fixture
def client(db):
    # raise_server_exceptions=False: assert on the 500 a real client would get.
    return TestClient(_app(db), raise_server_exceptions=False)


# ---- API: start/stop/restart failures -------------------------------------------------


def _raise(exc):
    def raiser(*args, **kwargs):
        raise exc

    return raiser


@pytest.mark.parametrize(
    ("exc", "status_code"),
    [
        (compose_manager.ComposeError("docker compose up -d failed for room-1: no such image"), 502),
        (compose_manager.ComposeTimeoutError("docker compose up -d timed out after 300s for room-1"), 504),
    ],
)
def test_compose_failure_keeps_its_message_and_marks_the_instance(client, db, monkeypatch, exc, status_code):
    monkeypatch.setattr(instances_router, "provision_and_start", _raise(exc))
    instance = _instance(db, rtsp_source="rtsp://cam/s")

    resp = client.post(f"/instances/{instance.id}/start")

    assert resp.status_code == status_code
    assert resp.json()["detail"] == str(exc)
    db.refresh(instance)
    assert (instance.status, instance.last_error) == (InstanceStatus.error, str(exc))
    (event,) = db.query(AuditEvent).all()
    assert (event.action, event.outcome) == (audit.INSTANCE_START, AuditOutcome.failure)


def test_unexpected_failure_is_not_echoed_to_the_client(client, db, monkeypatch, caplog):
    monkeypatch.setattr(instances_router.compose_manager, "down", _raise(KeyError("secret-internal-detail")))
    instance = _instance(db, status=InstanceStatus.running)

    resp = client.post(f"/instances/{instance.id}/stop")

    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert "secret-internal-detail" not in detail
    ref = detail.split("ref ")[1].rstrip(")")
    assert ref in caplog.text  # the reference ties the response to the logged traceback
    db.refresh(instance)
    assert instance.status == InstanceStatus.error


# ---- API: create ------------------------------------------------------------------------


@pytest.mark.parametrize("venue", ["Test", "has space", "-leading", "", "a/b", "../etc"])
def test_create_rejects_venues_docker_or_the_filesystem_would_choke_on(client, db, venue):
    resp = client.post("/instances", json={"venue": venue, "agent_id": "agent-1"})
    assert resp.status_code == 422
    assert db.query(Instance).count() == 0


def test_create_with_no_free_ports_is_a_conflict_not_a_500(client, db, monkeypatch):
    monkeypatch.setattr(instances_router, "allocate_port", _raise(NoPortsAvailableError("No free ports in range 9000-9000")))

    resp = client.post("/instances", json={"venue": "room-1", "agent_id": "agent-1"})

    assert resp.status_code == 409
    assert "No free ports" in resp.json()["detail"]
    (event,) = db.query(AuditEvent).all()
    assert event.outcome == AuditOutcome.failure


def test_create_losing_a_race_for_the_port_is_a_conflict(client, db, monkeypatch):
    taken = _instance(db, venue="room-a")
    monkeypatch.setattr(instances_router, "allocate_port", lambda db: taken.ui_port)

    resp = client.post("/instances", json={"venue": "room-b", "agent_id": "agent-1"})

    assert resp.status_code == 409
    assert db.query(Instance).count() == 1


# ---- API: dependencies down ---------------------------------------------------------


def test_details_report_docker_being_down_as_503(client, db, monkeypatch):
    docker_client = SimpleNamespace(containers=SimpleNamespace(list=_raise(DockerException("daemon not running"))))
    monkeypatch.setattr(compose_manager, "_docker_client", lambda: docker_client)
    instance = _instance(db)

    resp = client.get(f"/instances/{instance.id}")

    assert resp.status_code == 503
    assert "Docker is unavailable" in resp.json()["detail"]


def test_database_outage_is_a_json_503(client, db, monkeypatch):
    monkeypatch.setattr(db, "query", _raise(OperationalError("SELECT 1", {}, Exception("server has gone away"))))

    resp = client.get("/instances")

    assert resp.status_code == 503
    body = resp.json()
    assert body["detail"].startswith("Database unavailable")
    assert "gone away" not in resp.text
    assert body["error_id"]


def test_unhandled_exception_is_a_json_500_with_a_reference(db, caplog):
    app = _app(db)

    @app.get("/boom")
    def boom():
        raise RuntimeError("secret-internal-detail")

    resp = TestClient(app, raise_server_exceptions=False).get("/boom")

    assert resp.status_code == 500
    body = resp.json()
    assert body["detail"] == "Internal server error"
    assert body["error_id"] in caplog.text


# ---- compose_manager ------------------------------------------------------------------


def test_compose_timeout_and_missing_binary_become_compose_errors(monkeypatch):
    instance = SimpleNamespace(config_dir="/tmp/room-1", compose_project="pyca-room-1", venue="room-1")

    monkeypatch.setattr(subprocess, "run", _raise(subprocess.TimeoutExpired(cmd="docker", timeout=300)))
    with pytest.raises(compose_manager.ComposeTimeoutError, match="timed out"):
        compose_manager.up(instance)

    monkeypatch.setattr(subprocess, "run", _raise(FileNotFoundError("docker")))
    with pytest.raises(compose_manager.ComposeError, match="Could not run docker compose"):
        compose_manager.down(instance)


def test_compose_calls_are_bounded_by_the_configured_timeout(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(compose_manager.settings, "compose_timeout_seconds", 42)
    compose_manager.up(SimpleNamespace(config_dir="/tmp/x", compose_project="pyca-x", venue="x"))
    assert seen["timeout"] == 42


# ---- monitoring -------------------------------------------------------------------------


def test_dir_size_survives_files_vanishing_mid_walk(tmp_path, monkeypatch):
    (tmp_path / "kept.mkv").write_bytes(b"12345")
    (tmp_path / "vanishing.mkv").write_bytes(b"123")
    real_stat = type(tmp_path).stat

    def flaky_stat(self, *args, **kwargs):
        if self.name == "vanishing.mkv":
            raise FileNotFoundError(self)
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(type(tmp_path), "stat", flaky_stat)
    assert monitoring_router._dir_size_bytes(tmp_path) == 5
    assert monitoring_router._dir_size_bytes(tmp_path / "missing") == 0


def test_ready_reflects_the_decision_engine(client, monkeypatch):
    status = decision_engine.EngineStatus()
    monkeypatch.setattr(decision_engine, "status", status)

    resp = client.get("/monitoring/ready")
    assert resp.status_code == 503
    assert resp.json()["checks"]["decision_engine"]["error"] == "no poll has completed yet"
    assert resp.json()["checks"]["database"]["ok"] is True

    status.last_success_at = datetime.now(timezone.utc)
    assert client.get("/monitoring/ready").status_code == 200

    status.last_success_at = datetime.now(timezone.utc) - timedelta(hours=1)
    status.last_error = "Could not reach Opencast"
    resp = client.get("/monitoring/ready")
    assert resp.status_code == 503
    assert resp.json()["checks"]["decision_engine"]["error"] == "Could not reach Opencast"

    assert client.get("/monitoring/health").status_code == 200  # liveness stays unconditional


# ---- Opencast client ------------------------------------------------------------------


def _opencast(handler):
    oc = OpencastClient()
    oc._client = httpx.AsyncClient(base_url="http://opencast.test", transport=httpx.MockTransport(handler))
    return oc


def test_one_malformed_scheduled_event_does_not_hide_the_rest():
    end = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    start = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    results = [
        {"id": "mp-bad", "agent_id": "room-1", "technical_start": start},  # no technical_end
        {"id": "mp-good", "agent_id": "room-2", "technical_start": start, "technical_end": end},
    ]
    oc = _opencast(lambda request: httpx.Response(200, json={"results": results}))

    events = asyncio.run(oc.get_scheduled_events())

    assert [e.mediapackage_id for e in events] == ["mp-good"]


@pytest.mark.parametrize(
    "handler",
    [
        lambda request: httpx.Response(503, text="down"),
        lambda request: httpx.Response(200, text="<html>login</html>"),
        lambda request: httpx.Response(200, json=["not", "a", "dict"]),
        _raise(httpx.ConnectError("refused")),
    ],
)
def test_opencast_failures_surface_as_opencast_error(handler):
    with pytest.raises(OpencastError):
        asyncio.run(_opencast(handler).get_scheduled_events())


def test_unusable_agent_state_is_an_opencast_error_and_404_is_none():
    with pytest.raises(OpencastError):
        asyncio.run(_opencast(lambda request: httpx.Response(200, json={"unexpected": 1})).get_agent_state("a"))
    assert asyncio.run(_opencast(lambda request: httpx.Response(404)).get_agent_state("a")) is None


# ---- decision engine --------------------------------------------------------------------


def _event(agent_id):
    start = datetime.now(timezone.utc) + timedelta(minutes=5)
    return ScheduledEvent(
        mediapackage_id=f"mp-{agent_id}", title="Lecture", agent_id=agent_id, start=start, end=start + timedelta(hours=1)
    )


class _FakeOpencast:
    def __init__(self, events, states):
        self.events, self.states = events, states

    async def get_scheduled_events(self):
        return self.events

    async def get_agent_state(self, agent_id):
        state = self.states[agent_id]
        if isinstance(state, Exception):
            raise state
        return state


@pytest.fixture
def engine_db(db, monkeypatch):
    monkeypatch.setattr(decision_engine, "SessionLocal", lambda: db)
    monkeypatch.setattr(decision_engine, "_teardown_deferred_ids", set())
    return db


def _offline(agent_id):
    return AgentState(agent_id=agent_id, state="unknown", seconds_since_update=1e9, rtsp_source="rtsp://cam/s")


def test_one_venue_failing_does_not_block_the_others_or_teardown(engine_db, monkeypatch):
    _instance(engine_db, venue="room-idle", status=InstanceStatus.running)
    started, stopped = [], []

    def provision(instance):
        if instance.venue == "room-explodes":
            raise RuntimeError("unexpected")
        started.append(instance.venue)

    async def no_pending_ingest(instance):
        return False

    monkeypatch.setattr(decision_engine, "provision_and_start", provision)
    monkeypatch.setattr(decision_engine, "has_pending_ingest", no_pending_ingest)
    monkeypatch.setattr(decision_engine.compose_manager, "down", lambda instance: stopped.append(instance.venue))
    oc = _FakeOpencast(
        [_event("room-unreachable"), _event("room-explodes"), _event("room-ok")],
        {
            "room-unreachable": OpencastError("Opencast returned HTTP 500"),
            "room-explodes": _offline("room-explodes"),
            "room-ok": _offline("room-ok"),
        },
    )

    asyncio.run(decision_engine.run_once(oc))

    assert started == ["room-ok"]
    assert stopped == ["room-idle"]
    statuses = {i.venue: i.status for i in engine_db.query(Instance)}
    assert statuses == {
        "room-idle": InstanceStatus.stopped,
        "room-explodes": InstanceStatus.error,
        "room-ok": InstanceStatus.running,
    }


def test_instance_left_mid_stop_by_a_crash_is_finished_off(engine_db, monkeypatch):
    _instance(engine_db, venue="room-stuck", status=InstanceStatus.stopping)
    stopped = []

    async def no_pending_ingest(instance):
        return False

    monkeypatch.setattr(decision_engine, "has_pending_ingest", no_pending_ingest)
    monkeypatch.setattr(decision_engine.compose_manager, "down", lambda instance: stopped.append(instance.venue))

    asyncio.run(decision_engine.run_once(_FakeOpencast([], {})))

    assert stopped == ["room-stuck"]
    assert engine_db.query(Instance).one().status == InstanceStatus.stopped


def test_failing_to_fetch_the_schedule_fails_the_pass_and_touches_nothing(engine_db, monkeypatch):
    _instance(engine_db, venue="room-1", status=InstanceStatus.running)
    monkeypatch.setattr(decision_engine.compose_manager, "down", _raise(AssertionError("must not tear down")))

    class Down(_FakeOpencast):
        async def get_scheduled_events(self):
            raise OpencastError("Could not reach Opencast")

    with pytest.raises(OpencastError):
        asyncio.run(decision_engine.run_once(Down([], {})))
    assert engine_db.query(Instance).one().status == InstanceStatus.running
