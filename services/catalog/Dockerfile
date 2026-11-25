FROM python:3.12-slim

WORKDIR /app
COPY shared /shared
COPY services/catalog/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir /shared
COPY services/catalog/music_catalog ./music_catalog

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HF_HOME=/cache/hf HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin muse && mkdir -p /cache/hf && chown 10001 /cache/hf
USER 10001

CMD ["python", "-m", "music_catalog"]
