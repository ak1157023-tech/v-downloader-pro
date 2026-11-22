"""Upload and FFmpeg clip-processing API used by the ClipCut web UI."""
import os
import shutil
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse

router = APIRouter()
ROOT = Path(os.environ.get("CLIPCUT_DATA_DIR", tempfile.gettempdir())) / "clipecut_media"
ROOT.mkdir(parents=True, exist_ok=True)
MAX_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "2048")) * 1024 * 1024
SOURCES = {}
JOBS = {}
LOCK = threading.Lock()


def ffmpeg_path():
    return shutil.which("ffmpeg")


def probe(path):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        p = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=20, check=True)
        return float(p.stdout.strip())
    except Exception:
        return None


def file_info(path, name, kind, source_id):
    return {"id": source_id, "name": name, "kind": kind,
            "size": path.stat().st_size, "duration": probe(path) if kind == "video" else None}


@router.get("/api/config")
def config():
    return {"max_bytes": MAX_BYTES, "limit": f"{MAX_BYTES // (1024**3)} GB",
            "clip": bool(ffmpeg_path()), "ffmpeg": bool(ffmpeg_path())}


@router.post("/api/upload")
async def upload(kind: str = Form("video"), file: UploadFile = File(...)):
    if kind not in ("video", "image"):
        return JSONResponse({"error": "Unsupported upload type."}, status_code=400)
    suffix = Path(file.filename or "").suffix[:12]
    source_id = uuid.uuid4().hex
    folder = ROOT / "sources"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{source_id}{suffix}"
    size = 0
    try:
        with target.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_BYTES:
                    out.close()
                    target.unlink(missing_ok=True)
                    return JSONResponse({"error": "File is larger than this server allows."}, status_code=413)
                out.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
    if not target.exists() or target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        return JSONResponse({"error": "The uploaded file is empty."}, status_code=400)
    source = file_info(target, file.filename or target.name, kind, source_id)
    SOURCES[source_id] = {"path": target, **source}
    return source


@router.get("/api/source/{source_id}/stream")
def source_stream(source_id: str):
    source = SOURCES.get(source_id)
    if not source or not source["path"].is_file():
        return JSONResponse({"error": "Source file not found. Please upload it again."}, status_code=404)
    media = "image/*" if source["kind"] == "image" else "video/*"
    return FileResponse(source["path"], media_type=media, filename=source["name"],
                        content_disposition_type="inline")


def _public(job):
    return {k: job.get(k) for k in ("id", "status", "phase", "pct", "files", "error")}


def _run_clip(job, body, source):
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise RuntimeError("FFmpeg is not installed or not available in PATH.")
    outdir = ROOT / "jobs" / job["id"]
    outdir.mkdir(parents=True, exist_ok=True)
    src = source["path"]
    outputs = []
    if source["kind"] == "image":
        seconds = max(1, min(600, int(body.get("seconds") or 5)))
        out = outdir / "clip_1.mp4"
        cmd = [ffmpeg, "-y", "-loop", "1", "-framerate", "25", "-i", str(src),
               "-t", str(seconds), "-vf",
               "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
               "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
               "-movflags", "+faststart", str(out)]
        subprocess.run(cmd, capture_output=True, text=True, timeout=1800, check=True)
        outputs.append(out)
    else:
        clips = body.get("clips") or []
        if not clips:
            duration = source.get("duration")
            if not duration:
                raise ValueError("Could not read video duration. Please enter a valid start and end time.")
            clips = [{"start": 0, "end": min(float(duration), 30)}]
        for i, item in enumerate(clips[:30]):
            start, end = float(item.get("start", 0)), float(item.get("end", 0))
            if start < 0 or end <= start:
                raise ValueError("Each clip must have an end time after its start time.")
            out = outdir / f"clip_{i + 1}.mp4"
            job["phase"] = f"Cutting clip {i + 1} of {min(len(clips), 30)}"
            job["pct"] = round(i * 100 / max(1, min(len(clips), 30)))
            cmd = [ffmpeg, "-y", "-ss", str(start), "-i", str(src), "-t", str(end - start),
                   "-map", "0:v:0?", "-map", "0:a:0?", "-c:v", "libx264",
                   "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-b:a", "128k",
                   "-movflags", "+faststart", str(out)]
            subprocess.run(cmd, capture_output=True, text=True, timeout=1800, check=True)
            outputs.append(out)
    job["files"] = [{"i": i, "name": p.name, "size": p.stat().st_size,
                     "type": "video/mp4", "path": str(p)} for i, p in enumerate(outputs)]
    job["status"], job["pct"], job["phase"] = "ready", 100, "Done"


def _worker(job, body, source):
    try:
        job["status"], job["phase"] = "processing", "Starting FFmpeg"
        _run_clip(job, body, source)
    except Exception as exc:
        job["status"] = "error"
        msg = str(exc)
        if isinstance(exc, subprocess.CalledProcessError):
            msg = (exc.stderr or "FFmpeg could not process this file.")[-1200:]
        job["error"] = msg[:1200]


@router.post("/api/clip")
def create_clip(body: dict):
    source = SOURCES.get(str(body.get("source") or ""))
    if not source or not source["path"].is_file():
        return JSONResponse({"error": "Source file not found. Please upload it again."}, status_code=404)
    if not ffmpeg_path():
        return JSONResponse({"error": "FFmpeg is not installed or not available in PATH."}, status_code=503)
    job_id = uuid.uuid4().hex[:12]
    job = {"id": job_id, "status": "queued", "phase": "Waiting", "pct": 0,
           "files": [], "error": None}
    JOBS[job_id] = job
    threading.Thread(target=_worker, args=(job, body, source), daemon=True).start()
    return {"id": job_id}
