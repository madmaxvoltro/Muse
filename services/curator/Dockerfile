FROM python:3.12-slim

ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu

WORKDIR /app
COPY shared /shared
COPY services/curator/requirements.txt .
RUN pip install --no-cache-dir torch --index-url ${TORCH_INDEX} \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir "/shared[taste]"

COPY services/curator/*.py ./

CMD ["python", "main.py"]
