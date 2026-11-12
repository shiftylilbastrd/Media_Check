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
CMD ["python", "run.py"]
