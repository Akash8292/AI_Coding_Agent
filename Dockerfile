FROM python:3.11-slim

# git is used for repository status, checkpoints and cloning workspaces
RUN apt-get update && apt-get install -y --no-install-recommends git curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend ./backend
COPY frontend ./frontend

# Run as an unprivileged user; all mutable state lives under /app/data
RUN useradd --create-home --uid 1000 codesage \
    && mkdir -p /app/data/workspaces /app/data/index_cache \
    && chown -R codesage:codesage /app/data
USER codesage

ENV PYTHONPATH=/app/backend \
    PYTHONUNBUFFERED=1 \
    PORT=5000 \
    FLASK_ENV=production \
    DATABASE_URL=sqlite:////app/data/codesage.db \
    WORKSPACE_ROOT=/app/data/workspaces \
    INDEX_CACHE_DIR=/app/data/index_cache \
    OLLAMA_ENABLED=false

EXPOSE 5000
VOLUME ["/app/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://localhost:${PORT}/health" || exit 1

CMD ["gunicorn", "--chdir", "backend", "--config", "backend/gunicorn.conf.py", "wsgi:app"]
