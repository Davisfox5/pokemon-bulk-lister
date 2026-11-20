# Card Portfolio / bulk lister, for a host that runs containers.
# Build from this directory:  docker build -t bulk-lister .
FROM python:3.11-slim

# OpenCV needs these two system libraries.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt "gunicorn>=22"
COPY . .

# Everything the app writes (the SQLite file, card photos, CSVs, the eBay
# token, saved portal sessions, the session key) goes under output/ and
# input/. Both are links into /data, so mount a persistent disk at /data or
# every deploy starts empty.
ENV TRUST_PROXY=1 PORT=8080
VOLUME /data
EXPOSE 8080

# One process, several threads. The app keeps job state and locks in memory,
# so it must not be run with more than one worker.
CMD ["sh", "-c", "mkdir -p /data/output /data/input/grids && ln -sfn /data/output /app/output && ln -sfn /data/input /app/input && exec gunicorn --workers 1 --threads 8 --timeout 300 --bind 0.0.0.0:${PORT} webapp.app:app"]
