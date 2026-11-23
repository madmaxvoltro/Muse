# Small admin image: migrations, roles, status, backups, forget/export, key rotation.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY shared /shared
COPY services/admin/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir /shared \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin muse
COPY db/migrations /app/migrations
COPY services/admin/admin.py .
USER 10001
ENTRYPOINT ["python", "admin.py"]
CMD ["migrate"]
