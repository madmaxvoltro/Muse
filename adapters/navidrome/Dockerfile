FROM python:3.12-slim

WORKDIR /app
COPY adapters/navidrome/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY adapters/navidrome/main.py .

CMD ["python", "main.py"]
