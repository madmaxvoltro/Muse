FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY shared /shared
COPY services/ingestion-api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir /shared

COPY services/ingestion-api/app ./app

EXPOSE 8000
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin muse
USER 10001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--no-server-header"]
