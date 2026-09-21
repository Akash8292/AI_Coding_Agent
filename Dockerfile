FROM python:3.11-slim

# Install system dependencies including git
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install python dependencies
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# Copy backend and frontend source code
COPY backend ./backend
COPY frontend ./frontend

# Set Python path to include backend
ENV PYTHONPATH=/app/backend
ENV PORT=5000
ENV FLASK_ENV=production

EXPOSE 5000

# Run with Gunicorn using gevent/sync worker
CMD ["gunicorn", "--chdir", "backend", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "120", "wsgi:app"]
