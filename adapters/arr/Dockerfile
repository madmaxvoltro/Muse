FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY adapters/arr/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY adapters/arr/main.py .

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin muse
USER 10001
CMD ["python", "main.py"]
