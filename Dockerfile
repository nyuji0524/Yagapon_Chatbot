# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    API_PORT=8000 \
    YAGAPON_CONFIG_PATH=/data/server_config.json \
    YAGAPON_CATALOG_PATH=/data/knowledge-catalog.json \
    YAGAPON_RAG_DB_PATH=/data/rag.sqlite3 \
    YAGAPON_VOICEPRINT_DIR=/data/voiceprints \
    YAGAPON_RECORDING_DIR=/data/recordings

RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg libopus0 ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install \
    --disable-pip-version-check \
    --retries 10 \
    --timeout 120 \
    --requirement requirements.txt

COPY api ./api
COPY bot ./bot
COPY knowledge_sync ./knowledge_sync
COPY knowledge_catalog ./knowledge_catalog
COPY knowledge_curation ./knowledge_curation
COPY source_index ./source_index
COPY rag_eval ./rag_eval
COPY main.py ./

RUN useradd --create-home --uid 10001 yagapon \
    && mkdir -p /data/voiceprints /data/recordings /data/transcripts /data/knowledge-outbox \
    && chown -R yagapon:yagapon /app /data

USER yagapon

EXPOSE 8000
VOLUME ["/data"]

# The production VM is intentionally small. Cold Python imports and Discord startup
# can exceed one minute while an image is first paged into memory, so startup
# failures must not make Compose tear down otherwise healthy dependent workers.
HEALTHCHECK --interval=15s --timeout=15s --start-period=150s --retries=4 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=10).read()"

CMD ["python", "main.py"]
