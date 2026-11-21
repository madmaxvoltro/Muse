FROM python:3.12-slim

# CPU-only PyTorch by default (the CUDA wheel is gigabytes). For an NVIDIA GPU build with:
#   docker compose build --build-arg TORCH_INDEX=https://download.pytorch.org/whl/cu124 embedding-worker
# and give the service the GPU (see docker-compose.gpu.yml). MUSE_EMBED_DEVICE=auto picks it up.
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu

WORKDIR /app
COPY shared /shared
COPY services/embedding-worker/requirements.txt .
RUN pip install --no-cache-dir torch --index-url ${TORCH_INDEX} \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir "/shared[taste]"
COPY services/embedding-worker/*.py ./

CMD ["python", "worker.py"]
