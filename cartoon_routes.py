"""Clipecut AI Cartoon Studio - web routes (FastAPI). Mounted from app.py."""
from typing import Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import cartoon_engine as engine

router = APIRouter()


class CartoonRequest(BaseModel):
    story: str = Field(min_length=20, max_length=6000)
    lang: Literal["bn", "en"] = "bn"
    voice: Literal["male", "female"] = "male"
    style: Literal["2d", "3d"] = "2d"
    ratio: Literal["9:16", "16:9"] = "9:16"
    minutes: Literal["1", "3", "5"] = "1"


def _err(msg, code=400):
    return JSONResponse({"error": msg}, status_code=code)


@router.get("/api/cartoon/status")
def cartoon_status():
    return engine.status()


@router.post("/api/cartoon/generate")
def cartoon_generate(req: CartoonRequest):
    st = engine.status()
    if not st["ready"]:
        return _err("Missing on the server: " + ", ".join(st["missing"]))
    running = sum(1 for j in engine.JOBS.values() if j["status"] in ("queued", "processing")) 
    if running >= 3:
        return _err("The server is busy. Please try again in a few minutes.", 429)
    job = engine.start_job(req.model_dump())
    return {"id": job["id"]}
