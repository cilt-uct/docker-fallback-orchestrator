from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter(tags=["web"])

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
STATIC_DIR = ROOT_DIR / "static"

templates = Jinja2Templates(directory=ROOT_DIR / "templates" / "web")


def static_url(path: str) -> str:
    """URL for a static file, stamped with its mtime so browsers refetch it when it changes."""
    return f"/static/{path}?v={int((STATIC_DIR / path).stat().st_mtime)}"


templates.env.globals["static_url"] = static_url


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse(request, "index.html", {})


@router.get("/activity", response_class=HTMLResponse)
def audit_log(request: Request):
    return templates.TemplateResponse(request, "audit.html", {})
