FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    ffmpeg \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8200

# The image starts the app only. Schema is NOT applied automatically — run
#     docker run --rm --env-file .env <image> python db/migrate.py
# before (or in a one-shot migration container alongside) the app container.
#
# Health: /health answers 503 until the critical dependencies are ready, so this
# probe reflects real readiness rather than "the process is up".
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8200/health',timeout=4).status==200 else 1)"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8200", "--log-level", "info"]
