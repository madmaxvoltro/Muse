FROM python:3.12-slim

WORKDIR /app
COPY services/recommendation-api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/recommendation-api/app ./app

EXPOSE 8001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
