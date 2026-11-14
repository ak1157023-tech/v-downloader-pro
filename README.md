# V-Downloader Pro
Run locally:  pip install -r requirements.txt  &&  python app.py   -> http://127.0.0.1:5000
(Install FFmpeg for MP3 and merged HD video.)

Deploy (any Docker host, e.g. Render / Railway / VPS): the included Dockerfile installs FFmpeg and starts gunicorn.
Env vars: MAX_MB (default 2000), MAX_JOBS (default 3).
