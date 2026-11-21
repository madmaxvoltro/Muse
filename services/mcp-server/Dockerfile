FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY services/mcp-server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/mcp-server/server.py .
ENV MUSE_MCP_TRANSPORT=streamable-http MUSE_MCP_HOST=0.0.0.0 MUSE_MCP_PORT=8002
EXPOSE 8002
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin muse
USER 10001
CMD ["python", "server.py"]
