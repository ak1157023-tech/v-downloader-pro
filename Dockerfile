FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -r requirements.txt
CMD gunicorn -w 1 --threads 16 --timeout 0 -b 0.0.0.0:${PORT:-8000} app:app
