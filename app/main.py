import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates

from app.services.killfeed import get_kills

APP_NAME = os.getenv("APP_NAME", "EVE-World")

app = FastAPI(title=APP_NAME)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"app_name": APP_NAME})


@app.get("/api/kills")
async def kills():
    return await get_kills()


@app.get("/health")
def health():
    return {"status": "ok"}
