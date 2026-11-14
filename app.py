"""V-Downloader Pro - backend. Run: python app.py  (or gunicorn app:app)"""
import hashlib, hmac, ipaddress, os, shutil, socket, subprocess, tempfile, threading, time, uuid
from urllib.parse import urljoin, urlparse, unquote, quote

import requests
from datetime import timedelta

from flask import Flask, Response, jsonify, redirect, request, send_file, send_from_directory, session, stream_with_context
from werkzeug.middleware.proxy_fix import ProxyFix

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

MAX_BYTES = int(os.environ.get("MAX_MB", "2000")) * 1024 * 1024
MAX_JOBS = int(os.environ.get("MAX_JOBS", "3"))
FFMPEG = shutil.which("ffmpeg")
TIERS = {"480": 480, "720": 720, "1080": 1080, "2k": 1440, "4k": 2160}
LABELS = {"480": "480p", "720": "720p", "1080": "1080p", "2k": "2K", "4k": "4K"}
EXT = {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi", ".mp3", ".m4a", ".ogg", ".wav", ".flac"}
UA = {"User-Agent": "Mozilla/5.0 V-Downloader-Pro"}
JOBS, HITS = {}, {}
FONT = next((p for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                         "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf") if os.path.exists(p)), None)
POS = {"tl": "x=24:y=24", "tr": "x=w-tw-24:y=24", "bl": "x=24:y=h-th-24", "br": "x=w-tw-24:y=h-th-24"}

app = Flask(__name__, static_folder="static")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)  # real visitor IP behind Render's proxy

# ---------- optional password protection (set APP_PASSWORD on the server) ----------
PASSWORD = os.environ.get("APP_PASSWORD", "")
app.secret_key = os.environ.get("SECRET_KEY") or hashlib.sha256(("vdp-" + PASSWORD).encode()).hexdigest()
app.permanent_session_lifetime = timedelta(days=30)
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")))
FAILS = {}
LOGIN_PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>V-Downloader Pro - Login</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#060a16;color:#e8eefc;font-family:"Segoe UI",system-ui,sans-serif}
form{width:min(92vw,380px);background:#0d1426;border:1px solid #1f2b4a;border-radius:22px;padding:28px}
h1{margin:0 0 6px;font-size:1.5rem}p{margin:0 0 18px;color:#8b9bc2}
input{width:100%;box-sizing:border-box;padding:14px;border-radius:12px;border:2px solid #1f2b4a;background:#121b33;color:#e8eefc;font:inherit;font-size:1rem}
input:focus{outline:none;border-color:#3b82f6}
button{width:100%;margin-top:14px;padding:14px;border:0;border-radius:12px;background:linear-gradient(135deg,#1d4ed8,#3b82f6 55%,#38bdf8);color:#fff;font:inherit;font-weight:700;font-size:1rem;cursor:pointer}
.e{color:#f87171;min-height:22px;margin-top:10px}</style></head><body>
<form method="post"><h1>V-Downloader Pro</h1><p>Enter the password to continue.</p>
<input type="password" name="password" placeholder="Password" autofocus autocomplete="current-password">
<button type="submit">Log in</button><div class="e">__ERR__</div></form></body></html>"""


@app.before_request
def require_login():
    p = request.path
    if (not PASSWORD or p in ("/login", "/manifest.json", "/icon.svg")
            or (p.startswith("/google") and p.endswith(".html")) or session.get("ok")):
        return None
    if p.startswith("/api/"):
        return jsonify(error="Please log in first."), 401
    return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not PASSWORD:
        return redirect("/")
    err = ""
    if request.method == "POST":
        ip, now = request.remote_addr, time.time()
        FAILS[ip] = [t for t in FAILS.get(ip, []) if now - t < 900]
        if len(FAILS[ip]) >= 8:
            err = "Too many attempts. Try again in 15 minutes."
        elif hmac.compare_digest(request.form.get("password", "").encode(), PASSWORD.encode()):
            session.permanent = True
            session["ok"] = True
            return redirect("/")
        else:
            FAILS[ip].append(now)
            err = "Wrong password."
    return Response(LOGIN_PAGE.replace("__ERR__", err), mimetype="text/html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")



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


BASE = os.path.dirname(os.path.abspath(__file__))


@app.get("/")
def home():
    # index.html can sit next to app.py or inside a static/ folder
    folder = os.path.join(BASE, "static") if os.path.exists(os.path.join(BASE, "static", "index.html")) else BASE
    return send_from_directory(folder, "index.html")


@app.get("/<name>.html")
def google_verify(name):
    # serves Google Search Console verification files (googleXXXX.html) placed next to app.py
    path = os.path.join(BASE, name + ".html")
    if name.startswith("google") and os.path.isfile(path):
        return send_from_directory(BASE, name + ".html")
    return jsonify(error="Not found."), 404


# ---------- installable web app (PWA) ----------
@app.get("/manifest.json")
def manifest():
    return jsonify(
        name="V-Downloader Pro", short_name="V-Downloader", start_url="/", scope="/",
        display="standalone", background_color="#060a16", theme_color="#060a16",
        icons=[{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}],
    )


@app.get("/icon.svg")
def icon():
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
           '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
           '<stop offset="0" stop-color="#1d4ed8"/><stop offset="1" stop-color="#38bdf8"/></linearGradient></defs>'
           '<rect width="512" height="512" rx="110" fill="url(#g)"/>'
           '<path d="M256 110v190m0 0l-80-80m80 80l80-80M140 380h232" stroke="#fff" stroke-width="40" '
           'stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>')
    return Response(svg, mimetype="image/svg+xml")


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
            return jsonify(error="That file is larger than this server allows."), 400
        return jsonify(kind="direct", title=name, size=size, duration=None, thumbnail=None,
                       format=os.path.splitext(name)[1].lstrip(".") or ct.split("/")[-1],
                       qualities=[{"id": "original", "label": "Original quality", "sub": "Source file as is"}],
                       audio=bool(FFMPEG), watermark=bool(FFMPEG))
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
                   size=quals[-1].get("size"), qualities=quals, audio=bool(FFMPEG), watermark=bool(FFMPEG))


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


def clean_wm_text(wm):
    return "".join(c for c in str((wm or {}).get("text") or "") if c.isprintable())[:40].strip()


def apply_wm(j, wm):
    text = clean_wm_text(wm)
    if not text:
        raise ValueError("Enter the watermark name.")
    j["status"], j["pct"] = "processing", 100
    tf, out = os.path.join(j["dir"], "wm.txt"), os.path.join(j["dir"], "wm_out.mp4")
    with open(tf, "w", encoding="utf-8") as f:
        f.write(text)
    flt = "drawtext=textfile=%s:%sfontcolor=white@0.85:fontsize=h/24:box=1:boxcolor=black@0.35:boxborderw=10:%s" % (
        tf, f"fontfile={FONT}:" if FONT else "", POS.get(wm.get("pos"), POS["br"]))
    pr = subprocess.Popen([FFMPEG, "-y", "-i", j["path"], "-vf", flt, "-c:v", "libx264", "-preset", "veryfast",
                           "-crf", "23", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    while pr.poll() is None:
        if j["cancel"].is_set():
            pr.kill()
            raise Cancelled
        time.sleep(0.5)
    if pr.returncode:
        raise ValueError("Adding the watermark failed.")
    j["path"], j["name"] = out, os.path.splitext(j["name"])[0] + "_watermarked.mp4"


def run_direct(j, url, q, wm=None):
    if q != "mp3" and not wm:
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
                raise ValueError("File is larger than this server allows.")
            f.write(c)
            j["pct"] = got * 100 / total if total else 0
    r.close()
    if q == "mp3":
        j["status"] = "processing"
        convert(j, src, os.path.splitext(name)[0] + ".mp3")
    else:
        j["path"], j["name"] = src, name
        apply_wm(j, wm)


def run_extracted(j, url, q, wm=None):
    def hook(d):
        if j["cancel"].is_set():
            raise Cancelled
        if d["status"] == "downloading":
            t = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if t > MAX_BYTES:
                raise ValueError("File is larger than this server allows.")
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
    if wm and q != "mp3":
        apply_wm(j, wm)


def worker(j, fn, url, q, wm):
    try:
        fn(j, url, q, wm)
        j["status"] = "ready"
    except Exception as e:
        if j["cancel"].is_set():
            j["status"] = "cancelled"
        else:
            j["status"] = "error"
            j["error"] = str(e)[:200] if isinstance(e, ValueError) else "This download could not be prepared."


@app.post("/api/jobs")
def create_job():
    b = request.json or {}
    url, q, kind = (b.get("url") or "").strip(), b.get("quality"), b.get("kind")
    try:
        check_host(url)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    wm = b.get("wm") if isinstance(b.get("wm"), dict) and q != "mp3" else None
    if wm and (not FFMPEG or not clean_wm_text(wm)):
        return jsonify(error="Enter a watermark name (watermarks need FFmpeg on the server)."), 400
    if q == "mp3" and not FFMPEG:
        return jsonify(error="MP3 conversion is not available on this server."), 400
    if kind == "extracted" and not yt_dlp:
        return jsonify(error="Not supported."), 400
    if sum(j["status"] in ("queued", "downloading", "processing") for j in JOBS.values()) >= MAX_JOBS:
        return jsonify(error="Server is busy. Try again in a minute."), 429
    j = {"id": uuid.uuid4().hex, "status": "queued", "pct": 0, "error": None, "path": None, "name": None,
         "cancel": threading.Event(), "t": time.time(), "dir": tempfile.mkdtemp(prefix="vdp_")}
    JOBS[j["id"]] = j
    threading.Thread(target=worker, args=(j, run_extracted if kind == "extracted" else run_direct, url, q, wm),
                     daemon=True).start()
    return jsonify(id=j["id"])


@app.get("/api/jobs/<jid>")
def job_status(jid):
    j = JOBS.get(jid)
    if not j:
        return jsonify(error="Job not found."), 404
    return jsonify(status=j["status"], pct=round(j["pct"], 1), error=j["error"])


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


def janitor():
    while True:
        time.sleep(300)
        for k, j in list(JOBS.items()):
            if time.time() - j["t"] > 1800:
                shutil.rmtree(j["dir"], ignore_errors=True)
                JOBS.pop(k, None)


threading.Thread(target=janitor, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), threaded=True)
