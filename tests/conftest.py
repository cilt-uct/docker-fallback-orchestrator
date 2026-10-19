"""Fixtures shared by every test module.

Builders that tests call with arguments (make_instance, scheduled_event, ...)
are factory fixtures: request the fixture, then call it.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import docker
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# No test may reach a real Docker daemon; compose_manager's client is built from this.
docker.from_env = MagicMock()

from app import decision_engine  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.errors import register_exception_handlers  # noqa: E402
from app.models import Instance, InstanceStatus  # noqa: E402 - also registers the tables on Base
from app.opencast_client import AgentState, ScheduledEvent  # noqa: E402
from app.routers import audit, instances, monitoring, web  # noqa: E402


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def app(db):
    """The API's routers and error handlers, on the test database, without the lifespan's engine loop."""
    app = FastAPI()
    register_exception_handlers(app)
    for module in (instances, audit, monitoring, web):
        app.include_router(module.router)
    app.dependency_overrides[get_db] = lambda: db
    return app


@pytest.fixture
def client(app):
    # raise_server_exceptions=False: assert on the 500 a real client would get.
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def engine_db(db, monkeypatch):
    """Point the decision engine at the test database, with no deferred teardowns left from other tests."""
    monkeypatch.setattr(decision_engine, "SessionLocal", lambda: db)
    monkeypatch.setattr(decision_engine, "_teardown_deferred_ids", set())
    return db


@pytest.fixture
def make_instance(db):
    def make(venue="room-1", status=InstanceStatus.stopped, **kwargs):
        kwargs.setdefault("config_dir", f"/tmp/{venue}")
        instance = Instance(
            venue=venue,
            agent_id=venue,
            ui_port=9000 + db.query(Instance).count(),
            compose_project=f"pyca-{venue}",
            status=status,
            **kwargs,
        )
        db.add(instance)
        db.commit()
        return instance

    return make


@pytest.fixture
def scheduled_event():
    def make(agent_id="room-1", minutes_to_start=5):
        start = datetime.now(UTC) + timedelta(minutes=minutes_to_start)
        return ScheduledEvent(
            mediapackage_id=f"mp-{agent_id}",
            title="Lecture",
            agent_id=agent_id,
            start=start,
            end=start + timedelta(hours=1),
        )

    return make


@pytest.fixture
def offline_agent():
    def make(agent_id="room-1", rtsp_source="rtsp://cam/s"):
        return AgentState(
            agent_id=agent_id, state="unknown", seconds_since_update=1e9, rtsp_source=rtsp_source
        )

    return make


class FakeOpencast:
    """Stands in for OpencastClient. `states` maps agent_id to an AgentState, or an exception to raise."""

    def __init__(self, events, states=None):
        self.events = events
        self.states = states or {}

    async def get_scheduled_events(self):
        return self.events

    async def get_agent_state(self, agent_id):
        state = self.states.get(agent_id)
        if isinstance(state, Exception):
            raise state
        return state


@pytest.fixture
def fake_opencast():
    return FakeOpencast
