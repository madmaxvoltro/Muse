FROM python:3.12-slim

WORKDIR /app
COPY adapters/arr/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY adapters/arr/main.py .

CMD ["python", "main.py"]
