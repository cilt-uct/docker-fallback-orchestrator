import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import Base, engine
from app.decision_engine import run_forever
from app.errors import register_exception_handlers
from app.routers import audit, instances, monitoring, web

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _log_engine_exit(task: asyncio.Task) -> None:
    # run_forever only returns by being cancelled at shutdown; anything else means
    # backups have silently stopped being provisioned.
    if not task.cancelled() and task.exception() is not None:
        logger.critical("Decision engine stopped unexpectedly", exc_info=task.exception())


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail at startup with the actual cause, not later with an Opencast/MySQL login error.
    if missing := settings.placeholder_secrets():
        message = (
            f"{', '.join(missing)} still set to the placeholder - set real values in .env (see .env.example)"
        )
        if not settings.allow_placeholder_secrets:
            raise RuntimeError(message)
        logger.warning("%s; continuing because ALLOW_PLACEHOLDER_SECRETS is set", message)

    # TODO: replace with Alembic migrations once the schema stabilises.
    Base.metadata.create_all(bind=engine)

    task = asyncio.create_task(run_forever())
    task.add_done_callback(_log_engine_exit)
    app.state.engine_task = task
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - already logged by _log_engine_exit; don't fail shutdown
            pass


app = FastAPI(title="pyca-orchestrator", lifespan=lifespan)
register_exception_handlers(app)
app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parent.parent / "static"), name="static")
app.include_router(instances.router)
app.include_router(audit.router)
app.include_router(monitoring.router)
app.include_router(web.router)
