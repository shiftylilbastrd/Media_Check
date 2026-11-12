FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg util-linux \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV DB_PATH=/data/media-check-tools.db
ENV WEBHOOK_DEBUG_LOG=/data/webhook-raw.log

EXPOSE 8585
# --workers 1 is load-bearing, not just a size choice: run.py's create_app()
# starts the worker/scheduler background threads once at import time, so
# more than one worker process would mean more than one of each loop
# running in parallel. --threads gives the web UI concurrency without
# touching that invariant.
CMD ["gunicorn", "--bind", "0.0.0.0:8585", "--workers", "1", "--threads", "4", "--timeout", "60", "run:app"]
