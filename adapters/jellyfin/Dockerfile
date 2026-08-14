FROM python:3.12-slim

WORKDIR /app
COPY adapters/jellyfin/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY adapters/jellyfin/main.py .

CMD ["python", "main.py"]
