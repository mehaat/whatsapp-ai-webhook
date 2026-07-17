# ME-HAAT Fashion AI Bot v7.0 — production image
FROM python:3.12-slim

# System deps for psycopg2 + reportlab (Pillow) + healthcheck curl.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=5000

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application.
COPY . .

# Persistent data (SQLite, invoices, token store) lives here; mount a volume.
RUN mkdir -p /var/data
VOLUME ["/var/data"]

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:${PORT}/health/live || exit 1

# Single worker keeps the in-process job queue + rate limiters correct.
CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:${PORT} --workers 1 --threads 4 --timeout 60"]
