FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY services/mcp-server/pyproject.toml .
COPY services/mcp-server/muse_mcp ./muse_mcp
RUN pip install --no-cache-dir .
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin muse
USER 10001

EXPOSE 8003
# Shared HTTP endpoint: every client sends its own Authorization: Bearer <token>.
CMD ["muse-mcp", "--http", "--host", "0.0.0.0", "--port", "8003"]
