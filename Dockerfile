FROM denoland/deno:bin AS deno-bin
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg fontconfig fonts-liberation ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=deno-bin /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py automation.py shorts_clipper.py ./

ENV PORT=10000 MAX_WORKERS=1 MAX_HEIGHT=720 FFMPEG_THREADS=2
CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:${PORT:-10000} --workers 1 --threads 8 --timeout 0"]
