FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_DOWNLOADER_HOST=0.0.0.0 \
    HF_DOWNLOADER_PORT=8000

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt -i https://mirrors.volces.com/pypi/simple

COPY app ./app
COPY templates ./templates
COPY static ./static
COPY download.py ./download.py
COPY README.md ./README.md

RUN mkdir -p /app/data /app/logs /app/downloads /opt/tosutil \
    && useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app /opt/tosutil

USER appuser

VOLUME ["/app/data", "/app/logs", "/app/downloads", "/opt/tosutil"]

EXPOSE 8000

CMD ["python", "download.py"]
