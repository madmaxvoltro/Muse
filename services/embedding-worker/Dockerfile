FROM python:3.12-slim

WORKDIR /app
COPY shared /shared
COPY services/embedding-worker/requirements.txt .
# CPU-only PyTorch first: the default wheel drags in gigabytes of CUDA libraries the small
# embedding model (all-MiniLM-L6-v2) doesn't need.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir /shared
COPY services/embedding-worker/worker.py services/embedding-worker/deep_taste.py services/embedding-worker/taste_models.py services/embedding-worker/ranker_training.py ./

CMD ["python", "worker.py"]
