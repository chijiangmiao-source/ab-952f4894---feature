FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    VALVE_DB_DIR=/data

WORKDIR /app

# Dependencies first for better layer caching.
COPY app/requirements.txt /app/app/requirements.txt
COPY requirements-dev.txt /app/requirements-dev.txt
RUN pip install --no-cache-dir -r app/requirements.txt -r requirements-dev.txt

COPY app /app/app
COPY tests /app/tests
COPY scripts /app/scripts

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8080

# Container-level health probe (no curl in slim image; use the stdlib).
HEALTHCHECK --interval=5s --timeout=5s --start-period=5s --retries=12 \
  CMD python -c "import os,urllib.request,sys; port=os.environ.get('PORT','8080'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=3).status == 200 else 1)"

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
