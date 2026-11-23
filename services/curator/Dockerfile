FROM python:3.12-slim

ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY shared /shared
COPY services/curator/requirements.txt .
RUN pip install --no-cache-dir torch --index-url ${TORCH_INDEX} \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir "/shared[taste]"

COPY services/curator/*.py ./

ENV HF_HOME=/cache/hf HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin muse && mkdir -p /cache/hf && chown 10001 /cache/hf
USER 10001
CMD ["python", "main.py"]
