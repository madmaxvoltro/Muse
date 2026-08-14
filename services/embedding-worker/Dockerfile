FROM python:3.12-slim

WORKDIR /app
COPY shared /shared
COPY services/embedding-worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir /shared
COPY services/embedding-worker/worker.py .

CMD ["python", "worker.py"]
