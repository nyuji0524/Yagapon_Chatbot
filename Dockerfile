# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    API_PORT=8000 \
    YAGAPON_CONFIG_PATH=/data/server_config.json \
    YAGAPON_CATALOG_PATH=/data/knowledge-catalog.json \
    YAGAPON_VOICEPRINT_DIR=/data/voiceprints

RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg libopus0 ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --requirement requirements.txt

COPY api ./api
COPY bot ./bot
COPY knowledge_sync ./knowledge_sync
COPY knowledge_catalog ./knowledge_catalog
COPY main.py ./

RUN useradd --create-home --uid 10001 yagapon \
    && mkdir -p /data/voiceprints \
    && chown -R yagapon:yagapon /app /data

USER yagapon

EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read()"

CMD ["python", "main.py"]
