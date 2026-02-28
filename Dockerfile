FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NIGHTFEED_DATABASE_PATH=/app/data/rss_site_bridge.db \
    NIGHTFEED_START_SCHEDULER=1

WORKDIR /app

RUN mkdir -p /app/data

COPY pyproject.toml setup.py README.md ./
COPY rss_site_bridge ./rss_site_bridge
COPY wsgi.py ./

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "4", "--access-logfile", "-", "--error-logfile", "-", "--capture-output", "--log-level", "info", "wsgi:app"]
