FROM mcr.microsoft.com/playwright/python:v1.59.0-noble@sha256:d8d9811a0e7cfac967f0c2f55d12b739087ae4b0808577b794c2a29ed5124938

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY exporter.py .

ENTRYPOINT ["python", "exporter.py"]
