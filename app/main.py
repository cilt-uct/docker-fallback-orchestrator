import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.database import Base, engine
from app.decision_engine import run_forever
from app.routers import instances, monitoring, web

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # TODO: replace with Alembic migrations once the schema stabilises.
    Base.metadata.create_all(bind=engine)

    task = asyncio.create_task(run_forever())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="pyca-orchestrator", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parent.parent / "static"), name="static")
app.include_router(instances.router)
app.include_router(monitoring.router)
app.include_router(web.router)
