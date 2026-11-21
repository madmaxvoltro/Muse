FROM python:3.12-slim
WORKDIR /app
COPY services/mcp-server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/mcp-server/server.py .
ENV MUSE_MCP_TRANSPORT=streamable-http MUSE_MCP_HOST=0.0.0.0 MUSE_MCP_PORT=8002
EXPOSE 8002
CMD ["python", "server.py"]
