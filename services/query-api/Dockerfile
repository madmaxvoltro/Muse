FROM python:3.12-slim

WORKDIR /app
COPY shared /shared
COPY services/query-api/requirements.txt .
# CPU-only PyTorch, as in the embedding worker: semantic search embeds the query with the
# same small model the worker used for content.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir /shared
COPY services/query-api/app ./app

EXPOSE 8002
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8002"]
