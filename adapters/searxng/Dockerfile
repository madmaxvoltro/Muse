FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY adapters/searxng/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY adapters/searxng/main.py .

EXPOSE 8080
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin muse
USER 10001
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
