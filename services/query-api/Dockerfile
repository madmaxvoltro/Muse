FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY shared /shared
COPY services/query-api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir "/shared[taste]"
COPY services/query-api/query ./query

EXPOSE 8003
# --no-access-log: URLs carry user ids; logs are files too.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin muse
USER 10001
CMD ["uvicorn", "query.main:app", "--host", "0.0.0.0", "--port", "8003", "--no-access-log", "--no-server-header"]
