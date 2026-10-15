"""Last-resort exception handlers for the HTTP API.

Routers translate the failures they expect (compose errors, missing RTSP
source, ...) into specific responses themselves. These handlers catch what
is left, so a client always gets JSON in the same `{"detail": ...}` shape as
every other error, and never the internals of an exception we didn't plan for.
"""

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import InterfaceError, OperationalError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

logger = logging.getLogger("api")


def new_error_id() -> str:
    """Short reference returned to the client and logged, to tie a report to its traceback."""
    return uuid.uuid4().hex[:12]


async def _database_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
    error_id = new_error_id()
    # Connection-level failures (MySQL down, connection dropped) are worth retrying; the rest are bugs.
    unavailable = isinstance(exc, (OperationalError, InterfaceError, PoolTimeoutError))
    logger.error(
        "Database error [%s] on %s %s", error_id, request.method, request.url.path, exc_info=exc
    )
    if unavailable:
        return JSONResponse(
            status_code=503,
            content={"detail": "Database unavailable - try again shortly", "error_id": error_id},
            headers={"Retry-After": "5"},
        )
    return JSONResponse(status_code=500, content={"detail": "Internal server error", "error_id": error_id})


async def _unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    error_id = new_error_id()
    logger.error(
        "Unhandled error [%s] on %s %s", error_id, request.method, request.url.path, exc_info=exc
    )
    return JSONResponse(status_code=500, content={"detail": "Internal server error", "error_id": error_id})


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(SQLAlchemyError, _database_error)
    app.add_exception_handler(Exception, _unhandled_error)
