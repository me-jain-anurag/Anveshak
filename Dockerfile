# Anveshak — API, worker and monitor share this image.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1 \
    ANVESHAK_VAR_DIR=/var/anveshak \
    ANVESHAK_DATA_DIR=/app/data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY anveshak ./anveshak
COPY data ./data
RUN pip install -e . && useradd --system --home /app anveshak && mkdir -p /var/anveshak && chown anveshak /var/anveshak
USER anveshak

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/v1/health')" || exit 1
CMD ["anveshak", "serve", "--host", "0.0.0.0", "--port", "8000"]
