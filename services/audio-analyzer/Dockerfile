# CPU by default. For an NVIDIA GPU build with:
#   --build-arg TORCH_INDEX=https://download.pytorch.org/whl/cu124
# and run with docker-compose.gpu.yml (MUSE_DEVICE=cuda).
FROM python:3.12-slim

ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libsndfile1 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY shared /shared
COPY services/audio-analyzer/requirements.txt .
RUN pip install --no-cache-dir torch --index-url ${TORCH_INDEX} \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir /shared
COPY services/audio-analyzer/analyzer ./analyzer

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HF_HOME=/cache/hf HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin muse && mkdir -p /cache/hf && chown 10001 /cache/hf
USER 10001

EXPOSE 8004
CMD ["uvicorn", "analyzer.service:app", "--host", "0.0.0.0", "--port", "8004"]
