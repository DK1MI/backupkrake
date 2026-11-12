FROM python:3.13-slim-trixie

RUN apt-get update \
 && apt-get install -y --no-install-recommends rsync openssh-client restic tar tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

ENV BACKUPKRAKE_DATA_DIR=/data \
    BACKUPKRAKE_CONFIG_DIR=/config \
    PYTHONUNBUFFERED=1

VOLUME ["/data", "/config"]
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')"

# Runs as root on purpose: preserving the clients' file ownership (uid/gid) requires it.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
