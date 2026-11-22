"""ClipCut application entry point."""
import os
import shutil
import subprocess
import threading
import webbrowser
import zipfile
from pathlib import Path
from tempfile import NamedTemporaryFile

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

import cartoon_engine
from cartoon_routes import router as cartoon_router
from media_backend import router as media_router, JOBS as MEDIA_JOBS

BASE = Path(__file__).parent
app = FastAPI(title="ClipCut")
app.include_router(media_router)
app.include_router(cartoon_router)


@app.get("/")
def home():
    return FileResponse(BASE / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/icon.svg")
def icon():
    return FileResponse(BASE / "icon.svg", media_type="image/svg+xml")


@app.get("/api/system")
def system():
    path = shutil.which("ffmpeg")
    if not path:
        return {"ffmpeg": False, "version": None, "encoders": []}
    try:
        version = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=15).stdout.splitlines()[0]
        out = subprocess.run([path, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return {"ffmpeg": False, "version": None, "encoders": []}
    encoders = ["CPU x264 (H.264)" for _ in ["libx264"] if "libx264" in out]
    return {"ffmpeg": True, "version": version, "encoders": encoders}


@app.get("/api/jobs/{job_id}")
def job_info(job_id: str):
    job = MEDIA_JOBS.get(job_id)
    if job:
        return {k: job.get(k) for k in ("id", "status", "phase", "pct", "files", "error")}
    job = cartoon_engine.JOBS.get(job_id)
    if job:
        return cartoon_engine.public(job)
    return JSONResponse({"error": "Job not found."}, status_code=404)


@app.delete("/api/jobs/{job_id}")
def job_cancel(job_id: str):
    job = MEDIA_JOBS.get(job_id)
    if job:
        job["status"] = "cancelled"
        return {"ok": True}
    job = cartoon_engine.JOBS.get(job_id)
    if job:
        cartoon_engine.cancel_job(job)
    return {"ok": True}


@app.get("/api/jobs/{job_id}/files/{index}")
def job_file(job_id: str, index: int, inline: int = 0):
    job = MEDIA_JOBS.get(job_id) or cartoon_engine.JOBS.get(job_id)
    if not job or job.get("status") != "ready" or not (0 <= index < len(job.get("files", []))):
        return JSONResponse({"error": "File not found."}, status_code=404)
    f = job["files"][index]
    path = Path(f.get("path") or (Path(job["dir"]) / f["name"]))
    if not path.is_file():
        return JSONResponse({"error": "File not found."}, status_code=404)
    return FileResponse(path, media_type=f.get("type", "application/octet-stream"),
                        filename=f["name"], content_disposition_type="inline" if inline else "attachment")


@app.get("/api/jobs/{job_id}/file")
def legacy_job_file(job_id: str):
    return job_file(job_id, 0, 0)


@app.get("/api/jobs/{job_id}/zip")
def job_zip(job_id: str):
    job = MEDIA_JOBS.get(job_id) or cartoon_engine.JOBS.get(job_id)
    if not job or job.get("status") != "ready":
        return JSONResponse({"error": "Job is not ready."}, status_code=404)
    tmp = NamedTemporaryFile(prefix="clipecut_", suffix=".zip", delete=False)
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as archive:
        for f in job.get("files", []):
            path = Path(f.get("path") or (Path(job["dir"]) / f["name"]))
            if path.is_file():
                archive.write(path, f["name"])
    return FileResponse(tmp.name, media_type="application/zip", filename="clipecut_files.zip")


@app.get("/api/jobs/{job_id}/thumb/{index}")
def job_thumb(job_id: str, index: int):
    return JSONResponse({"error": "Thumbnail is not available for this output."}, status_code=404)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    on_server = "PORT" in os.environ
    if not on_server:
        threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    uvicorn.run(app, host="0.0.0.0" if on_server else "127.0.0.1", port=port)
