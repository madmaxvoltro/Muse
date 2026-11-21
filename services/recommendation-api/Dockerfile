FROM python:3.12-slim

WORKDIR /app
COPY shared /shared
COPY services/recommendation-api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir "/shared[taste]"
COPY services/recommendation-api/app ./app

EXPOSE 8001
# --no-access-log: request URLs contain user ids and ?mood= text; logs are files too.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001", "--no-access-log", "--no-server-header"]
