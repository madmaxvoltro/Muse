FROM python:3.12-slim

WORKDIR /app
COPY services/mcp-server/pyproject.toml .
COPY services/mcp-server/muse_mcp ./muse_mcp
RUN pip install --no-cache-dir .

EXPOSE 8003
# Shared HTTP endpoint: every client sends its own Authorization: Bearer <token>.
CMD ["muse-mcp", "--http", "--host", "0.0.0.0", "--port", "8003"]
