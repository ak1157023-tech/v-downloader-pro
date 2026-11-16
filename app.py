"""V-Downloader Pro - backend. Run: python app.py  (or gunicorn app:app)"""
import ipaddress, os, re, shutil, socket, subprocess, tempfile, threading, time, uuid, zipfile
from urllib.parse import urljoin, urlparse, unquote, quote

import requests
from flask import Flask, Response, jsonify, request, send_file, send_from_directory, stream_with_context

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

MAX_MB = int(os.environ.get("MAX_MB", "2048"))
MAX_BYTES = MAX_MB * 1024 * 1024
LIMIT_TXT = f"{MAX_MB / 1024:g} GB"
MAX_SOURCES = int(os.environ.get("MAX_SOURCES", "4"))
MAX_JOBS = int(os.environ.get("MAX_JOBS", "3"))
FFMPEG = shutil.which("ffmpeg")
TIERS = {"480": 480, "720": 720, "1080": 1080, "2k": 1440, "4k": 2160}
LABELS = {"480": "480p", "720": "720p", "1080": "1080p", "2k": "2K", "4k": "4K"}
EXT = {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi", ".mp3", ".m4a", ".ogg", ".wav", ".flac"}
UA = {"User-Agent": "Mozilla/5.0 V-Downloader-Pro"}
JOBS, HITS = {}, {}

app = Flask(__name__, static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = MAX_BYTES + (8 << 20)  # file + multipart overhead


class Cancelled(Exception):
    pass


# ---------- safety: only public http(s) hosts, no redirects into private networks ----------
def check_host(url):
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("Enter a valid http(s) link.")
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80))
    except socket.gaierror:
        raise ValueError("Could not find that website.")
    if any(not ipaddress.ip_address(i[4][0]).is_global for i in infos):
        raise ValueError("That address is not allowed.")


def open_url(url):
    for _ in range(6):
        check_host(url)
        r = requests.get(url, stream=True, allow_redirects=False, timeout=(8, 20), headers=UA)
        if r.status_code in (301, 302, 303, 307, 308):
            url = urljoin(url, r.headers.get("Location", ""))
            r.close()
            continue
        if r.status_code >= 400:
            r.close()
            raise ValueError(f"The website answered with error {r.status_code}.")
        return r, url
    raise ValueError("Too many redirects.")


def is_media(r, url):
    ct = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    ok = ct.startswith(("video/", "audio/")) or ct == "application/ogg" or (
        ct in ("application/octet-stream", "binary/octet-stream") and ext in EXT)
    return ok, ct


def filename(r, url):
    cd = r.headers.get("Content-Disposition", "")
    if "filename*=" in cd:
        return unquote(cd.split("filename*=")[1].split("''")[-1].strip('"; '))
    if "filename=" in cd:
        return cd.split("filename=")[1].strip('"; ')
    return unquote(os.path.basename(urlparse(url).path)) or "video"


@app.before_request
def rate_limit():
    if request.path.startswith("/api/"):
        now, ip = time.time(), request.remote_addr
        HITS[ip] = [t for t in HITS.get(ip, []) if now - t < 60] + [now]
        if len(HITS[ip]) > 300:
            return jsonify(error="Too many requests. Please wait a minute."), 429


@app.get("/")
def home():
    base = os.path.dirname(os.path.abspath(__file__))  # index.html next to app.py wins; static/ still works
    return send_from_directory(base if os.path.exists(os.path.join(base, "index.html")) else os.path.join(base, "static"), "index.html")


# ---------- info ----------
def short(f):
    w, h = f.get("width"), f.get("height")
    return min(w, h) if w and h else (h or 0)


def tiers_for(info):
    fm = [f for f in info.get("formats", []) if f.get("vcodec") not in (None, "none")
          and (FFMPEG or f.get("acodec") not in (None, "none"))]
    out = {}
    for k, v in TIERS.items():
        c = [f for f in fm if v * 0.9 <= short(f) <= v * 1.15]
        if c:
            out[k] = max(c, key=lambda f: (short(f), f.get("tbr") or 0))
    return out, fm


@app.post("/api/info")
def info():
    url = ((request.json or {}).get("url") or "").strip()
    try:
        r, final = open_url(url)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except requests.RequestException:
        return jsonify(error="Could not reach that link."), 400

    ok, ct = is_media(r, final)
    if ok:
        size = int(r.headers.get("Content-Length") or 0) or None
        name = filename(r, final)
        r.close()
        if size and size > MAX_BYTES:
            return jsonify(error=f"Failed: file is larger than the {LIMIT_TXT} limit."), 400
        return jsonify(kind="direct", title=name, size=size, duration=None, thumbnail=None,
                       format=os.path.splitext(name)[1].lstrip(".") or ct.split("/")[-1],
                       qualities=[{"id": "original", "label": "Original quality", "sub": "Source file as is"}],
                       audio=bool(FFMPEG))
    r.close()
    if not yt_dlp:
        return jsonify(error="This is not a direct video file link."), 400
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True, "skip_download": True}) as y:
            d = y.extract_info(url, download=False)
    except Exception:
        return jsonify(error="This link is not a supported video source."), 400

    tiers, fm = tiers_for(d)
    quals = [{"id": k, "label": LABELS[k], "size": f.get("filesize") or f.get("filesize_approx")}
             for k, f in tiers.items()]
    if not quals:
        best = max((short(f) for f in fm), default=0)
        quals = [{"id": "best", "label": "Best available", "sub": f"{best}p" if best else "Audio only"}]
    return jsonify(kind="extracted", title=d.get("title") or "Untitled", thumbnail=d.get("thumbnail"),
                   duration=d.get("duration_string"), format=d.get("ext"),
                   size=quals[-1].get("size"), qualities=quals, audio=bool(FFMPEG))


# ---------- direct streaming (original file, no server storage) ----------
@app.get("/api/direct")
def direct():
    try:
        r, final = open_url(request.args.get("url", ""))
    except (ValueError, requests.RequestException) as e:
        return jsonify(error=str(e)), 400
    ok, ct = is_media(r, final)
    size = int(r.headers.get("Content-Length") or 0)
    if not ok or size > MAX_BYTES:
        r.close()
        return jsonify(error="This file cannot be downloaded here."), 400

    def gen():
        got = 0
        try:
            for c in r.iter_content(1 << 16):
                got += len(c)
                if got > MAX_BYTES:
                    break
                yield c
        finally:
            r.close()

    h = {"Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename(r, final))}
    if size:
        h["Content-Length"] = str(size)
    return Response(stream_with_context(gen()), mimetype=ct, headers=h)


# ---------- jobs: server-side prepare (quality selection / MP3), then download ----------
def convert(j, src, outname):
    out = os.path.join(j["dir"], "out.mp3")
    p = subprocess.run([FFMPEG, "-y", "-i", src, "-vn", "-codec:a", "libmp3lame", "-b:a", "192k", out],
                       capture_output=True)
    if p.returncode:
        raise ValueError("Audio conversion failed.")
    j["path"], j["name"] = out, outname


def run_direct_audio(j, url, q):
    if q != "mp3":
        raise ValueError("Use the direct download for the original file.")
    r, final = open_url(url)
    ok, _ = is_media(r, final)
    if not ok:
        raise ValueError("Not a media file.")
    name = filename(r, final)
    total = int(r.headers.get("Content-Length") or 0)
    src, got = os.path.join(j["dir"], "src"), 0
    j["status"] = "downloading"
    with open(src, "wb") as f:
        for c in r.iter_content(1 << 16):
            if j["cancel"].is_set():
                raise Cancelled
            got += len(c)
            if got > MAX_BYTES:
                raise ValueError(f"Failed: file is larger than the {LIMIT_TXT} limit.")
            f.write(c)
            j["pct"] = got * 100 / total if total else 0
    r.close()
    j["status"] = "processing"
    convert(j, src, os.path.splitext(name)[0] + ".mp3")


def run_extracted(j, url, q):
    def hook(d):
        if j["cancel"].is_set():
            raise Cancelled
        if d["status"] == "downloading":
            t = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if t > MAX_BYTES:
                raise ValueError(f"Failed: file is larger than the {LIMIT_TXT} limit.")
            if t:
                j["pct"] = d["downloaded_bytes"] * 100 / t
        elif d["status"] == "finished":
            j["status"], j["pct"] = "processing", 100

    o = {"quiet": True, "noplaylist": True, "progress_hooks": [hook],
         "outtmpl": os.path.join(j["dir"], "%(title).80s.%(ext)s")}
    if FFMPEG:
        o["ffmpeg_location"], o["merge_output_format"] = FFMPEG, "mp4"
    with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True}) as y:
        d = y.extract_info(url, download=False)
    if q == "mp3":
        o["format"] = "bestaudio/best"
        o["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
    elif q in TIERS:
        f = tiers_for(d)[0].get(q)
        if not f:
            raise ValueError("That quality is not available for this video.")
        fid = f["format_id"]
        o["format"] = fid if f.get("acodec") not in (None, "none") else f"{fid}+bestaudio/{fid}"
    else:
        o["format"] = "bestvideo*+bestaudio/best" if FFMPEG else "best"
    j["status"] = "downloading"
    with yt_dlp.YoutubeDL(o) as y:
        y.download([url])
    files = [os.path.join(j["dir"], x) for x in os.listdir(j["dir"])]
    if not files:
        raise ValueError("No file was produced.")
    j["path"] = max(files, key=os.path.getsize)
    j["name"] = os.path.basename(j["path"])


def worker(j, fn, url, q):
    try:
        fn(j, url, q)
        j["status"] = "ready"
    except Exception as e:
        if j.get("keep"):
            shutil.rmtree(j["dir"], ignore_errors=True)
        if j["cancel"].is_set():
            j["status"] = "cancelled"
        else:
            j["status"] = "error"
            j["error"] = str(e)[:200] if isinstance(e, ValueError) else "This download could not be prepared."


def start_job(fn, arg, q=None, keep=False):
    if sum(j["status"] in ("queued", "downloading", "processing") for j in JOBS.values()) >= MAX_JOBS:
        return None
    j = {"id": uuid.uuid4().hex, "status": "queued", "pct": 0, "error": None, "path": None, "name": None,
         "cancel": threading.Event(), "t": time.time(), "dir": tempfile.mkdtemp(prefix="vdp_"),
         "keep": keep, "source": None, "files": []}
    JOBS[j["id"]] = j
    threading.Thread(target=worker, args=(j, fn, arg, q), daemon=True).start()
    return j


@app.post("/api/jobs")
def create_job():
    b = request.json or {}
    url, q, kind = (b.get("url") or "").strip(), b.get("quality"), b.get("kind")
    try:
        check_host(url)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    if q == "mp3" and not FFMPEG:
        return jsonify(error="MP3 conversion is not available on this server."), 400
    if kind == "extracted" and not yt_dlp:
        return jsonify(error="Not supported."), 400
    j = start_job(run_extracted if kind == "extracted" else run_direct_audio, url, q)
    if not j:
        return jsonify(error="Server is busy. Try again in a minute."), 429
    return jsonify(id=j["id"])


@app.get("/api/jobs/<jid>")
def job_status(jid):
    j = JOBS.get(jid)
    if not j:
        return jsonify(error="Job not found."), 404
    return jsonify(status=j["status"], pct=round(j["pct"], 1), error=j["error"], source=j["source"],
                   files=[{"i": i, "name": f["name"], "size": f["size"]} for i, f in enumerate(j["files"])])


@app.delete("/api/jobs/<jid>")
def job_cancel(jid):
    j = JOBS.get(jid)
    if j:
        j["cancel"].set()
    return jsonify(ok=True)


@app.get("/api/jobs/<jid>/file")
def job_file(jid):
    j = JOBS.get(jid)
    if not j or j["status"] != "ready":
        return jsonify(error="File is not ready."), 404
    return send_file(j["path"], as_attachment=True, download_name=j["name"])


# ---------- Clip: sources (uploaded or from a link) -> clips ----------
SOURCES = {}
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
AUDIO_EXT = {".mp3", ".m4a", ".ogg", ".wav", ".flac"}


def clean_name(n, default="file"):
    n = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", os.path.basename(n or "")).strip(" .")
    return n[:120] or default


def hsize(b):
    for u in ("B", "KB", "MB", "GB"):
        if b < 1024 or u == "GB":
            return f"{b:.1f} {u}" if u != "B" else f"{b} B"
        b /= 1024


def probe(path):
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                             capture_output=True, text=True, timeout=20).stdout.strip()
        return float(out)
    except Exception:
        return None


def drop_source(sid):
    s = SOURCES.pop(sid, None)
    if s:
        shutil.rmtree(s["dir"], ignore_errors=True)


def add_source(path, name, kind, folder):
    while len(SOURCES) >= MAX_SOURCES:  # keep disk use small: forget the oldest
        drop_source(min(SOURCES, key=lambda k: SOURCES[k]["t"]))
    sid = uuid.uuid4().hex
    s = {"id": sid, "path": path, "name": name, "kind": kind, "dir": folder, "t": time.time(),
         "size": os.path.getsize(path), "duration": probe(path) if kind != "image" else None}
    SOURCES[sid] = s
    return {k: s[k] for k in ("id", "name", "kind", "size", "duration")}


@app.errorhandler(413)
def too_large(_e):
    return jsonify(error=f"Failed: file is larger than the {LIMIT_TXT} limit."), 413


@app.get("/api/config")
def config():
    return jsonify(max_bytes=MAX_BYTES, limit=LIMIT_TXT, clip=bool(FFMPEG))


@app.post("/api/upload")
def upload():
    f = request.files.get("file")
    kind = "image" if request.form.get("kind") == "image" else "video"
    if not f or not f.filename:
        return jsonify(error="No file received."), 400
    name = clean_name(f.filename)
    ext = os.path.splitext(name)[1].lower()
    mt = (f.mimetype or "").lower()
    ok = (ext in IMG_EXT or mt.startswith("image/")) if kind == "image" else (
        ext in EXT or mt.startswith(("video/", "audio/")))
    if not ok:
        return jsonify(error="This file type is not supported."), 400
    folder = tempfile.mkdtemp(prefix="vdp_src_")
    path = os.path.join(folder, "src" + (ext or (".jpg" if kind == "image" else ".mp4")))
    try:
        f.save(path)
        size = os.path.getsize(path)
        if size > MAX_BYTES:
            raise ValueError(f"Failed: file is {hsize(size)}, the limit is {LIMIT_TXT}.")
        k = "image" if kind == "image" else ("audio" if os.path.splitext(path)[1] in AUDIO_EXT else "video")
        return jsonify(add_source(path, name, k, folder))
    except ValueError as e:
        shutil.rmtree(folder, ignore_errors=True)
        return jsonify(error=str(e)), 413
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        return jsonify(error="Upload failed."), 500


def run_source(j, url, _q):
    """Download a link (direct file or yt-dlp source) so it can be clipped."""
    r, final = open_url(url)
    ok, _ = is_media(r, final)
    j["status"] = "downloading"
    if ok:
        name = clean_name(filename(r, final), "video")
        ext = os.path.splitext(name)[1].lower() or ".mp4"
        total = int(r.headers.get("Content-Length") or 0)
        if total > MAX_BYTES:
            r.close()
            raise ValueError(f"Failed: file is {hsize(total)}, the limit is {LIMIT_TXT}.")
        path, got = os.path.join(j["dir"], "src" + ext), 0
        with open(path, "wb") as fh:
            for c in r.iter_content(1 << 16):
                if j["cancel"].is_set():
                    raise Cancelled
                got += len(c)
                if got > MAX_BYTES:
                    raise ValueError(f"Failed: file is larger than the {LIMIT_TXT} limit.")
                fh.write(c)
                j["pct"] = got * 100 / total if total else 0
        r.close()
    else:
        r.close()
        if not yt_dlp:
            raise ValueError("This is not a direct video file link.")

        def hook(d):
            if j["cancel"].is_set():
                raise Cancelled
            if d["status"] == "downloading":
                t = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                if t > MAX_BYTES:
                    raise ValueError(f"Failed: file is larger than the {LIMIT_TXT} limit.")
                if t:
                    j["pct"] = d["downloaded_bytes"] * 100 / t

        o = {"quiet": True, "noplaylist": True, "progress_hooks": [hook], "max_filesize": MAX_BYTES,
             "outtmpl": os.path.join(j["dir"], "%(title).80s.%(ext)s"),
             "format": "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b" if FFMPEG else "b[height<=1080]/b"}
        if FFMPEG:
            o["ffmpeg_location"], o["merge_output_format"] = FFMPEG, "mp4"
        with yt_dlp.YoutubeDL(o) as y:
            y.download([url])
        files = [os.path.join(j["dir"], x) for x in os.listdir(j["dir"]) if not x.endswith((".part", ".ytdl"))]
        if not files:
            raise ValueError(f"Could not download this link (it may be over the {LIMIT_TXT} limit).")
        path = max(files, key=os.path.getsize)
        name = clean_name(os.path.basename(path))
        if os.path.getsize(path) > MAX_BYTES:
            raise ValueError(f"Failed: file is larger than the {LIMIT_TXT} limit.")
    kind = "audio" if os.path.splitext(path)[1].lower() in AUDIO_EXT else "video"
    j["source"] = add_source(path, name, kind, j["dir"])


@app.post("/api/source/link")
def source_link():
    url = ((request.json or {}).get("url") or "").strip()
    try:
        check_host(url)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    j = start_job(run_source, url, keep=True)
    if not j:
        return jsonify(error="Server is busy. Try again in a minute."), 429
    return jsonify(id=j["id"])


@app.get("/api/source/<sid>/stream")
def source_stream(sid):
    s = SOURCES.get(sid)
    if not s:
        return jsonify(error="File expired. Please add it again."), 404
    s["t"] = time.time()
    return send_file(s["path"], conditional=True)


def ffrun(j, cmd):
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    while p.poll() is None:
        if j["cancel"].is_set():
            p.kill()
            p.wait()
            raise Cancelled
        time.sleep(0.3)
    return p.returncode == 0


def clip_cmd(s, a, b, mode, out):
    d, src = f"{b - a:.3f}", s["path"]
    mp4 = ["-movflags", "+faststart"] if out.endswith(".mp4") else []
    if s["kind"] == "image":
        return [FFMPEG, "-y", "-loop", "1", "-i", src, "-t", d, "-vf",
                "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p", "-r", "30", "-c:v", "libx264",
                "-preset", "veryfast"] + mp4 + [out]
    head = [FFMPEG, "-y", "-ss", f"{a:.3f}", "-i", src, "-t", d]
    if s["kind"] == "audio":
        return head + ["-vn", "-c", "copy", out]
    m = ["-map", "0:v:0?", "-map", "0:a:0?"]
    if mode == "fast":  # no re-encoding: finishes in seconds
        return head + m + ["-c", "copy", "-avoid_negative_ts", "make_zero"] + mp4 + [out]
    return head + m + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-c:a", "aac", "-b:a", "160k"] + mp4 + [out]


def run_clip(j, body, _q):
    s = SOURCES.get(body["source"])
    if not s or not os.path.exists(s["path"]):
        raise ValueError("The source file expired. Please add it again.")
    sext = os.path.splitext(s["path"])[1].lower()
    base = os.path.splitext(s["name"])[0][:60] or "clip"
    mode, clips = body["mode"], body["clips"]
    j["status"] = "processing"
    for n, (a, b) in enumerate(clips, 1):
        if s["kind"] == "audio":
            ext = sext
        elif s["kind"] == "video" and mode == "fast" and sext in (".mkv", ".webm"):
            ext = sext
        else:
            ext = ".mp4"
        label = "video" if s["kind"] == "image" else f"clip{n}"
        out = os.path.join(j["dir"], f"{base}_{label}{ext}")
        ok = ffrun(j, clip_cmd(s, a, b, mode, out))
        if not ok and mode == "fast" and s["kind"] == "video":  # codec not copyable -> re-encode this one
            out = os.path.splitext(out)[0] + ".mp4"
            ok = ffrun(j, clip_cmd(s, a, b, "precise", out))
        if not ok or not os.path.exists(out) or os.path.getsize(out) == 0:
            raise ValueError(f"Clip {n} could not be created. Check the start and end times.")
        j["files"].append({"name": os.path.basename(out), "path": out, "size": os.path.getsize(out)})
        j["pct"] = n * 100 / len(clips)


@app.post("/api/clip")
def create_clip():
    b = request.json or {}
    if not FFMPEG:
        return jsonify(error="Clip needs FFmpeg, which is not installed on this server."), 400
    s = SOURCES.get(b.get("source"))
    if not s:
        return jsonify(error="The source file expired. Please add it again."), 400
    mode = "precise" if b.get("mode") == "precise" else "fast"
    try:
        if s["kind"] == "image":
            d = float(b.get("seconds") or 5)
            if not 1 <= d <= 600:
                raise ValueError
            clips = [(0.0, d)]
        else:
            clips = []
            for c in (b.get("clips") or [])[:20]:
                a, e = float(c["start"]), float(c["end"])
                if a < 0 or e <= a or e - a > 6 * 3600:
                    raise ValueError
                if s.get("duration") and a >= s["duration"]:
                    return jsonify(error="Start time is after the end of the video."), 400
                clips.append((a, e))
            if not clips:
                raise ValueError
    except (ValueError, TypeError, KeyError):
        return jsonify(error="Check your start and end times."), 400
    j = start_job(run_clip, {"source": s["id"], "clips": clips, "mode": mode})
    if not j:
        return jsonify(error="Server is busy. Try again in a minute."), 429
    return jsonify(id=j["id"])


@app.get("/api/jobs/<jid>/files/<int:i>")
def job_clip_file(jid, i):
    j = JOBS.get(jid)
    if not j or j["status"] != "ready" or not 0 <= i < len(j["files"]):
        return jsonify(error="File is not ready."), 404
    return send_file(j["files"][i]["path"], as_attachment=True, download_name=j["files"][i]["name"])


@app.get("/api/jobs/<jid>/zip")
def job_zip(jid):
    j = JOBS.get(jid)
    if not j or j["status"] != "ready" or not j["files"]:
        return jsonify(error="File is not ready."), 404
    zp = os.path.join(j["dir"], "clips.zip")
    if not os.path.exists(zp):
        tmp = zp + f".{uuid.uuid4().hex[:6]}.tmp"
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED, allowZip64=True) as z:  # video is already compressed
            for f in j["files"]:
                z.write(f["path"], f["name"])
        os.replace(tmp, zp)
    return send_file(zp, as_attachment=True, download_name="clips.zip")


# ---------- timeline editor export: keep the chosen parts, join them into one video ----------
def has_audio(path):
    exe = shutil.which("ffprobe")
    if not exe:
        return True
    try:
        r = subprocess.run([exe, "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                            "-of", "csv=p=0", path], capture_output=True, text=True, timeout=20)
        return bool(r.stdout.strip())
    except Exception:
        return True


def run_edit(j, body, _q):
    s = SOURCES.get(body["source"])
    if not s or not os.path.exists(s["path"]):
        raise ValueError("The source file expired. Please add it again.")
    segs = body["segments"]
    total = sum(b - a for a, b in segs)
    aud = has_audio(s["path"])
    cmd, flt = [FFMPEG, "-y"], []
    for i, (a, b) in enumerate(segs):  # one fast-seeking input per part keeps picture and sound in sync
        cmd += ["-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", s["path"]]
        flt.append(f"[{i}:v:0]setpts=PTS-STARTPTS[v{i}]")
        if aud:
            flt.append(f"[{i}:a:0]aresample=48000,asetpts=PTS-STARTPTS[a{i}]")
    ins = "".join(f"[v{i}][a{i}]" if aud else f"[v{i}]" for i in range(len(segs)))
    flt.append(f"{ins}concat=n={len(segs)}:v=1:a={1 if aud else 0}[v]" + ("[a]" if aud else ""))
    base = os.path.splitext(s["name"])[0][:60] or "video"
    out = os.path.join(j["dir"], f"{base}_edited.mp4")
    cmd += ["-filter_complex", ";".join(flt), "-map", "[v]"] + (["-map", "[a]"] if aud else [])
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p"]
    cmd += (["-c:a", "aac", "-b:a", "160k"] if aud else []) + ["-movflags", "+faststart", "-progress", "pipe:1", "-nostats", out]
    j["status"] = "processing"
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    for line in p.stdout:
        if j["cancel"].is_set():
            p.kill()
            p.wait()
            raise Cancelled
        if line.startswith("out_time_us="):
            try:
                j["pct"] = min(99, int(line.split("=")[1]) / 1e6 / total * 100)
            except (ValueError, ZeroDivisionError):
                pass
    p.wait()
    if p.returncode or not os.path.exists(out) or os.path.getsize(out) == 0:
        raise ValueError("The edited video could not be created.")
    j["files"].append({"name": os.path.basename(out), "path": out, "size": os.path.getsize(out)})
    j["pct"] = 100


@app.post("/api/edit")
def create_edit():
    b = request.json or {}
    if not FFMPEG:
        return jsonify(error="Editing needs FFmpeg, which is not installed on this server."), 400
    s = SOURCES.get(b.get("source"))
    if not s or s["kind"] != "video":
        return jsonify(error="The source file expired. Please add it again."), 400
    try:
        segs = []
        for a, e in (b.get("segments") or [])[:100]:
            a, e = float(a), float(e)
            if a < 0 or e - a < 0.05 or (s.get("duration") and a >= s["duration"]):
                raise ValueError
            segs.append((a, e))
        if not segs:
            raise ValueError
    except (ValueError, TypeError):
        return jsonify(error="Check the edited timeline and try again."), 400
    j = start_job(run_edit, {"source": s["id"], "segments": segs})
    if not j:
        return jsonify(error="Server is busy. Try again in a minute."), 429
    return jsonify(id=j["id"])


def janitor():
    while True:
        time.sleep(300)
        for k, j in list(JOBS.items()):
            if time.time() - j["t"] > 1800:
                if not j.get("keep"):
                    shutil.rmtree(j["dir"], ignore_errors=True)
                JOBS.pop(k, None)
        for k, s in list(SOURCES.items()):
            if time.time() - s["t"] > 7200:
                drop_source(k)


threading.Thread(target=janitor, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), threaded=True)
