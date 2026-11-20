FROM python:3.12-slim

WORKDIR /app
COPY shared /shared
COPY services/recommendation-api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir /shared
COPY services/recommendation-api/app ./app
# Applied on start-up (app/migrate.py), so existing installs never need a manual psql step.
COPY db/migrations /migrations

EXPOSE 8001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
