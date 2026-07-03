FROM mcr.microsoft.com/playwright/python:v1.61.0-noble@sha256:a9731514f24121d1dcd25d58d0a38146646d290a5998fd80d3e533e7b5e21c69

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY exporter.py .

ENTRYPOINT ["python", "exporter.py"]
