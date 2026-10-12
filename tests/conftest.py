from unittest.mock import MagicMock

import docker
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# app.compose_manager builds a Docker client at import time; tests must not
# need a running Docker daemon.
docker.from_env = MagicMock()

from app.database import Base  # noqa: E402
import app.models  # noqa: E402, F401 - registers the tables on Base


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()
