FROM python:3.12-slim

WORKDIR /app

COPY shared /shared
COPY services/ingestion-api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir /shared

COPY services/ingestion-api/app ./app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
